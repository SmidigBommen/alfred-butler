# Alfred the Butler

Alfred is a collection of local, deterministic tools that an AI assistant can
compose when a task needs reasoning. Routine work stays in code so it is fast,
repeatable, testable, and inexpensive.

The first tool family is private web evidence collection:

```text
search -> localhost SearXNG -> candidate URLs
fetch -> public URL guard + readable-text extraction -> page evidence
research -> search + rank/deduplicate/diversify + fetch -> evidence bundle
```

SearXNG remains an unmodified upstream service. Alfred owns the stable JSON
contract, so the backend can be upgraded or replaced without changing every
consumer.

## Requirements

- Python 3.11 or newer
- Podman
- curl, for the health check

No third-party Python packages are required at runtime.

Development uses the Ruff version pinned in the `dev` optional dependencies.

## Install for Alfred

Install the web commands and their implicitly triggerable Codex skill with:

```bash
make install-agent
```

This installs the repository as an editable `uv` tool, exposing `alfred-notes`,
`alfred-web-search`, `alfred-web-fetch`, and `alfred-web-research` on the user
path. It also copies the version-controlled `alfred-personal-memory` and
`alfred-search-web` skills into `${CODEX_HOME:-~/.codex}/skills`. Start a new
Codex session after the first installation so skill discovery can load them.
Re-run `make install-agent` after changing skill instructions.

The skill chooses the smallest useful workflow: fetch a supplied page, search
and verify a direct fact, or collect a diverse evidence bundle for a broader
research question. Alfred then synthesizes the untrusted evidence into a concise
answer with direct links.

## Start the search service

The checked-in configuration pins SearXNG `2026.7.15-7b2199ecd` by immutable
image digest and exposes it only on `127.0.0.1:8888`.

```bash
make search-up
make search-health
```

Other lifecycle commands:

```bash
make search-status
make search-logs
make search-restart
make search-down
```

The local `infra/searxng/.env` contains the instance secret and is ignored by
Git. Use `.env.example` when setting up another machine.

## Search

```bash
PYTHONPATH=src python3 -m alfred_tools.web.search \
  --query "SearXNG official documentation" \
  --category it \
  --language en \
  --time-range month \
  --safe-search 1 \
  --limit 10
```

The command writes normalized JSON to stdout. Errors are JSON on stderr, with
exit code `1` for invalid input and `2` for backend failures.

Exact searches are cached for five minutes in
`.cache/alfred/search.sqlite3`. Use `--no-cache` for a fresh request or set
`ALFRED_SEARCH_CACHE` to choose another location. Set `ALFRED_SEARXNG_URL` to
use another private instance.

## Fetch

```bash
alfred-web-fetch \
  --url "https://docs.python.org/3/library/http.client.html" \
  --max-chars 12000
```

Fetch returns normalized JSON containing the final URL, metadata, compact text,
redirect history, retrieval time, truncation state, and a SHA-256 hash of the
response body. Identical requests are cached for one hour in
`.cache/alfred/fetch.sqlite3`; use `--no-cache` or `ALFRED_FETCH_CACHE` as with
search.

The fetcher accepts public HTTP(S) pages containing HTML, plain text, XHTML, or
JSON. It resolves and validates every destination, pins the connection to a
validated public IP, revalidates every redirect, verifies HTTPS certificates,
and bounds redirects, bytes, characters, and time. Private, loopback,
link-local, reserved, and mixed public/private DNS answers are rejected.
Unsupported content types and compressed responses are also rejected rather
than expanded. Exit code `3` identifies a safety-policy rejection; input and
backend errors retain exit codes `1` and `2`.

## Research

```bash
alfred-web-research \
  --query "site:pypi.org/project/ruff latest Ruff release" \
  --query "site:github.com/astral-sh/ruff/releases latest Ruff release" \
  --max-sources 4 \
  --max-chars-per-source 12000
```

Research is a deterministic orchestration layer, not a second AI. It searches
up to eight focused queries, combines duplicate URLs, ranks candidates,
diversifies domains, safely fetches bounded evidence, skips short or recognizable
challenge pages, and records partial search/fetch failures as warnings. Its JSON
bundle remains marked `untrusted`; Alfred supplies the judgment, comparison,
synthesis, and citations.

## Notes and personal memory

Alfred stores personal knowledge as portable Markdown in
`~/.local/share/alfred/notes/` by default. Set `ALFRED_NOTES_DIR` to use another
location. Each note has TOML metadata, a stable ID, global contextual labels,
and ordinary wiki links. The derived `.alfred-index.sqlite3` file provides
full-text search and a weighted relationship graph; it can always be rebuilt
from Markdown.

```bash
alfred-notes capture \
  --title "Continuous improvement" \
  --body "Always improve and have fun doing it." \
  --kind preference \
  --label personal \
  --label principles

alfred-notes search --query "improvement" --label principles
alfred-notes related NOTE_ID
alfred-notes review
```

The graph represents labels as first-class nodes, avoiding a dense pairwise edge
for every note sharing a common context. Explicit wiki links receive stronger
weight than two-hop shared-label relationships. Search and graph results include
separate text, label, graph, importance, and recency score components so recall
remains explainable. Links created by the CLI use stable note IDs, preventing
duplicate or renamed titles from silently changing graph targets. Manual
title-only links resolve only when the title is unique.

The `alfred-personal-memory` skill recalls relevant context and automatically
captures durable preferences, decisions, ideas, corrections, and recurring
working steps without using the internet. Automatic captures enter a review
queue. Alfred asks before storing sensitive personal information and refuses to
store credentials. The `sensitive` marker is a policy gate, not independent
encryption. Use `show`, `update --accept`, `link`, `archive`, `export`, and
`rebuild-index` to inspect and maintain the knowledge base. An explicit
`delete NOTE_ID --confirm` permanently removes a note and its derived graph data.

## Tests and development

```bash
make lint
make test
ALFRED_LIVE_TESTS=1 PYTHONPATH=src python3 -m unittest -v tests.test_live_search
```

Run both deterministic quality gates with `make check`. Apply Python import and
formatting fixes with `make format`.

Development follows red/green/refactor TDD:

1. Red: express one behavior with a focused failing test and run it.
2. Green: make the smallest production change that satisfies the test.
3. Refactor: improve structure while keeping the whole suite green.

Every bug fix starts with a regression test. Live tests supplement unit tests;
they do not replace deterministic transport fixtures.

## Safety and maintenance

- Only HTTP(S) result URLs are emitted; executable URL schemes are discarded.
- Responses are limited to 2 MB and requests default to a 15-second timeout.
- Search snippets and fetched page bodies are untrusted data. The tools never
  execute page content or instructions; the assistant must preserve that
  boundary during synthesis.
- Personal notes stay local and must never be inserted into web requests unless
the user explicitly requests that exact disclosure. Credentials belong in a
  password manager, not Alfred's notes.
- Search providers still receive the server's IP address and queries. A private
  SearXNG instance improves control but does not provide anonymity.
- SearXNG is AGPL-3.0-or-later. If we modify or publicly serve it, review the
  corresponding source-availability obligations before deployment.

To update SearXNG, pull a candidate image, inspect its version and digest, run
the full unit and live suites, then change `SEARXNG_IMAGE` in the Makefile.
