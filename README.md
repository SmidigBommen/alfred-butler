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
- FFmpeg, for normalizing explicit remote microphone recordings

The foundational tools use only the Python standard library. Local speech input
is an optional `faster-whisper` extra.

Development uses the Ruff version pinned in the `dev` optional dependencies.

## Install for Alfred

Install the web commands and their implicitly triggerable Codex skill with:

```bash
make install-agent
```

This installs the repository as an editable `uv` tool, exposing `alfred-notes`,
`alfred-web-search`, `alfred-web-fetch`, `alfred-web-research`, and
`alfred-weather` on the user path. It also copies the version-controlled
`alfred-personal-memory`, `alfred-search-web`, and `alfred-weather` skills into
`${CODEX_HOME:-~/.codex}/skills`. Start a new Codex session after the first
installation so skill discovery can load them. Re-run `make install-agent`
after changing skill instructions.

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

## Weather forecast

```bash
alfred-weather --location "Oslo" --days 5 --hours 24
```

The command resolves a city, region, or named public place through OpenStreetMap
Nominatim, rounds its coordinates to four decimals, and requests the compact
Locationforecast forecast from MET Norway. Supplying `--latitude` and
`--longitude` together bypasses place lookup. Output is bounded normalized JSON
with current conditions, up to 48 hourly entries, up to nine daily summaries,
the applied IANA time zone, cache state, and explicit provider and license
attribution. Input errors use exit code `1`; place-lookup and forecast failures
use exit code `2`.

Named places are cached for 30 days. Forecasts are cached until MET Norway's
`Expires` time and revalidated with `If-Modified-Since`. Alfred identifies itself
to both providers, serializes Nominatim lookups to at most one request per second,
and does not implement autocomplete or bulk geocoding. Normal use should keep the
cache enabled; `--no-cache` is for deliberate live checks. Place names and
coordinates leave this computer, so use cities and public places rather than
private addresses unless that disclosure is intentional.

Set `ALFRED_NOMINATIM_URL` to switch to another compatible geocoder without a
code change. `ALFRED_MET_FORECAST_URL` can point at a compatible MET forecast
proxy, and `ALFRED_WEATHER_USER_AGENT` can override the identifying project
string. Provider URLs must be absolute HTTP(S) URLs without embedded credentials.

User-specific defaults belong in `~/.config/alfred/preferences.env`, which should
be readable only by its owner:

```text
ALFRED_TIME_ZONE=Region/City
ALFRED_WEATHER_LOCATION=Municipality, Country
```

`ALFRED_TIME_ZONE` uses an IANA zone and defaults to `UTC`; daylight-saving
offsets are applied by the standard timezone database. If no location flag is
given, `ALFRED_WEATHER_LOCATION` supplies the private default. Process environment
variables override the file. Alfred parses only these allowlisted preference
keys as inert data—it never sources or executes the file. Do not commit it.

Weather data is provided by [MET Norway](https://api.met.no/) under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Named-place lookup is
provided by [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org/) with
© OpenStreetMap contributors attribution. Alfred is not an official Yr, NRK, or
MET Norway product and does not use Yr branding.

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

## Local assistant interface

Alfred's handler composes the deterministic memory, web, and weather tools
through a bounded model tool-calling loop. The model receives only registered
JSON tools; there is no shell tool. The browser interface is bound to localhost
and supports ordinary text plus English push-to-talk.

The server owns one provider-independent system prompt for both LM Studio and
OpenAI chat. Alfred responds tersely by default in a composed, discreet,
understated British-butler persona and prioritizes practical applications,
concrete next actions, and materially useful caveats. Explicit requests for
detail, safety, and correctness take precedence over brevity. This conversational
persona is separate from the Cedar speech voice and does not weaken tool,
privacy, source, or approval rules.

Install the interface with local speech support and start it with:

```bash
make install-voice
make serve
```

`make install-voice` records local speech as an enabled capability in
`~/.config/alfred/voice.enabled`. Subsequent `make install-agent` updates detect
that marker and preserve the `voice` extra instead of silently replacing the
tool with its lightweight text-only dependency set. Remove the marker before an
`install-agent` run only when intentionally returning to text-only operation.

Then open `http://127.0.0.1:8123`. Hold `Shift+CapsLock` while the page is focused,
speak, and release to transcribe and send. The on-screen button provides the same
behavior. Microphone transcription and spoken replies have independent provider
selectors. Local transcription and silent replies remain the defaults. Browser
speech synthesis provides local spoken output. The browser hotkey is page-scoped;
a native desktop helper is needed before it can be system-wide.

Alfred replies render a deliberately limited Markdown subset: headings, tables,
lists, block quotes, fenced and inline code, emphasis, rules, and HTTP(S) links.
The server converts Markdown to an inert rendering tree and the browser creates
DOM elements with text content; raw model HTML is never inserted into the page.
Wide tables scroll horizontally on smaller screens. User messages and errors
remain literal text.

When a model uses tools, its reply includes a collapsed, ephemeral **Activity &
sources** panel. Web entries show the exact queries, completion status, elapsed
time, cache markers, warnings, and the candidate or fetched source URLs returned
by Alfred. Weather entries show the resolved public place, MET Norway provider,
cache state, and exact forecast URL without duplicating the hourly forecast body.
The panel never includes page bodies, model prompts, credentials, or
private-memory contents, and it disappears with the browser session. Alfred also
instructs models to cite exact tool-returned URLs rather than rewriting them.

Local requests use LM Studio's Responses API at `http://127.0.0.1:1234/v1`. On
the first request Alfred uses an already-loaded model. If there is none, it
starts the server on loopback, selects the smallest installed model marked as
trained for tool use, and loads it as `alfred-local` with full GPU offload and a
30-minute idle TTL. Alfred never downloads an LM Studio model. Set
`ALFRED_LMSTUDIO_MODEL` to an installed model key to override automatic
selection. `ALFRED_LMSTUDIO_URL` may change the port but must remain loopback.
Local model responses allow 600 seconds by default because loading and warming a
large model can exceed the remote API's shorter deadline. Set
`ALFRED_LMSTUDIO_TIMEOUT` to a value from 1 through 3600 seconds to override it;
this changes each model-response deadline, not the bounded number of tool rounds.

English transcription uses `faster-whisper`. Its model defaults to the compact
quantized `small.en` and is downloaded into the normal model cache on first use;
subsequent use is local. If LM Studio has filled GPU memory, transcription
automatically retries on CPU. Override the model with `ALFRED_STT_MODEL`, and
tune execution with `ALFRED_STT_DEVICE` and `ALFRED_STT_COMPUTE_TYPE`. Start with
`alfred-serve --no-voice` for text-only operation.

Remote AI is opt-in and appears as a separate provider only when both variables
are set:

```bash
export OPENAI_API_KEY="..."
export ALFRED_OPENAI_MODEL="your-chosen-model-id"
alfred-serve
```

With `OPENAI_API_KEY` present, the microphone selector also offers explicit
remote transcription using `gpt-4o-mini-transcribe`, and spoken replies offer
OpenAI's `gpt-4o-mini-tts` with the Cedar voice and a natural, understated modern
British English delivery. Cedar is the only remote output voice: the policy is
owned by the server rather than selected by the browser. Override the model
defaults with `ALFRED_OPENAI_TRANSCRIBE_MODEL` and `ALFRED_OPENAI_TTS_MODEL`.
Remote microphone
selection locally decodes the browser recording with FFmpeg, converts it to a
16 kHz mono PCM WAV, and uploads that normalized recording to OpenAI. Temporary
input and output files are deleted before the request continues. Remote spoken
replies send the answer text to OpenAI. Neither follows the chat-model selector
automatically.
See OpenAI's [speech-to-text](https://developers.openai.com/api/docs/guides/speech-to-text)
and [text-to-speech](https://developers.openai.com/api/docs/guides/text-to-speech)
guides for the upstream APIs.

Model IDs are configurable rather than silently tracking a `latest` alias.
Switching providers starts a new session, so local conversation context cannot
be sent remotely by accident. Remote models are not offered personal-memory
tools, so they cannot retrieve or write private notes. Requests use `store:
false`; session messages live only in bounded process memory, expire after one
hour, and disappear when Alfred stops. Only a local `memory_capture` tool call
creates a durable note, retaining the sensitive-content and credential gates.
Audio requests and generated WAV responses are size- and time-bounded. Alfred
does not retain remote recordings, transcripts, or generated speech. Audio
normalization uses a private temporary directory and removes it on success or
failure.

For privacy-safe audio troubleshooting, run either:

```bash
ALFRED_DEBUG=1 alfred-serve
alfred-serve --debug
```

Diagnostics go to stderr and include providers, MIME types, byte counts, decoder
status, elapsed time, and transcript length. They never include raw audio,
transcript text, request bodies, or API keys.

## Tests and development

```bash
make lint
make test
ALFRED_LIVE_TESTS=1 PYTHONPATH=src python3 -m unittest -v tests.test_live_search
ALFRED_LIVE_TESTS=1 PYTHONPATH=src python3 -m unittest -v tests.test_live_weather
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
- Named-place weather requests disclose the place to OpenStreetMap and rounded
  coordinates to MET Norway. Never send a private address without explicit user
  intent. Keep caching, provider identification, rate limiting, and attribution
  intact.
- SearXNG is AGPL-3.0-or-later. If we modify or publicly serve it, review the
  corresponding source-availability obligations before deployment.

To update SearXNG, pull a candidate image, inspect its version and digest, run
the full unit and live suites, then change `SEARXNG_IMAGE` in the Makefile.
