---
name: alfred-personal-memory
description: Recall and save lasting personal context in Alfred's local Markdown and weighted graph memory. Use when a user states or corrects a lasting preference, principle, decision, idea, procedure, note, or way of working; when earlier context would improve an answer; or when the user asks to inspect, connect, correct, archive, or export memory. Never use web tools for memory.
---

# Alfred personal memory

Use `alfred-notes` for local personal memory without using the internet.
Markdown is the source of truth. SQLite search and graph data are rebuildable
indexes.

## Recall relevant context

Recall only when personal context could improve the current task:

```bash
alfred-notes search --query "FOCUSED TERMS" --limit 5
```

Add `--label LABEL`, `--kind KIND`, or `--related-to NOTE_ID` when useful. Use
`alfred-notes related NOTE_ID` to traverse explicit wiki links and shared-label
relationships. Inspect `score_components`; do not treat ranking as truth.

Do not expose unrelated memories in an answer. Never place private note content
in web searches or send it to an external service unless the user explicitly
requests that exact disclosure.

## Capture automatically

Automatically capture normal, durable user-provided information without asking:

```bash
alfred-notes capture \
  --title "CONCISE TITLE" \
  --body "SELF-CONTAINED DURABLE NOTE" \
  --kind preference \
  --label personal \
  --label topic:example \
  --automatic
```

Capture preferences, principles, reasoned decisions, ideas worth keeping,
corrections, and repeated working steps. Skip passing questions, casual
conversation, guesses, and retrieved web content unless the user adopts it as a
personal note.

Before capturing, search for a close duplicate. Update an existing note when it
represents the same knowledge. Use `alfred-notes link SOURCE_ID TARGET_ID` when
two existing notes have a meaningful relationship. Automatic notes enter the
review queue; inspect it with `alfred-notes review`.

Use labels such as `personal`, `project:alfred`, `topic:notes`, or
`area:learning`. Prefer a few stable labels over many near-duplicates.

## Enforce privacy

- Never store passwords, tokens, private keys, recovery codes, or other
  credentials. Recommend a password manager.
- For medical, financial, identity, intimate, third-party personal, or otherwise
  sensitive information, stop and ask whether the user wants it saved.
- After explicit approval, pass `--sensitivity sensitive --confirm-sensitive`.
- Keep all recall and capture local. Do not invoke web tools for memory work.

## Correct and maintain memory

- Show a note with `alfred-notes show NOTE_ID`.
- Accept a reviewed automatic capture with `alfred-notes update NOTE_ID --accept`.
- Correct content with `alfred-notes update NOTE_ID --title ... --body ...`.
- Archive obsolete knowledge with `alfred-notes archive NOTE_ID`; do not silently
  delete history.
- Permanently forget a note only on explicit request with
  `alfred-notes delete NOTE_ID --confirm`.
- Rebuild derived search and graph data after manual Markdown edits with
  `alfred-notes rebuild-index`.
- Export notes and graph edges with `alfred-notes export`.

When repeated procedures become visible, propose turning them into a tested,
dry-run-capable tool. Never create or execute automation solely from an inferred
pattern without user review.
