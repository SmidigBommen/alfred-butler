"""Command-line interface for Alfred's local notes and graph memory."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, TextIO

from alfred_tools.notes.store import VALID_KINDS, NoteStore, SensitiveContentError


def default_notes_root() -> Path:
    configured = os.environ.get("ALFRED_NOTES_DIR")
    return (
        Path(configured).expanduser() if configured else Path.home() / ".local/share/alfred/notes"
    )


def _add_limit(parser: argparse.ArgumentParser, default: int = 10) -> None:
    parser.add_argument("--limit", type=int, default=default)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Alfred's local Markdown graph memory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="create a local Markdown note")
    capture.add_argument("--title", required=True)
    capture.add_argument("--body", required=True)
    capture.add_argument("--label", action="append", default=[])
    capture.add_argument("--kind", choices=sorted(VALID_KINDS), default="note")
    capture.add_argument("--provenance")
    capture.add_argument("--importance", type=float, default=0.5)
    capture.add_argument("--automatic", action="store_true")
    capture.add_argument("--sensitivity", choices=("normal", "sensitive"), default="normal")
    capture.add_argument("--confirm-sensitive", action="store_true")

    show = subparsers.add_parser("show", help="show one note")
    show.add_argument("note_id")

    search = subparsers.add_parser("search", help="search notes and explain ranking")
    search.add_argument("--query", default="")
    search.add_argument("--label", action="append", default=[])
    search.add_argument("--kind", choices=sorted(VALID_KINDS))
    search.add_argument("--related-to")
    _add_limit(search)

    related = subparsers.add_parser("related", help="traverse weighted graph relationships")
    related.add_argument("note_id")
    _add_limit(related)

    link = subparsers.add_parser("link", help="add a durable wiki link between notes")
    link.add_argument("source_id")
    link.add_argument("target_id")

    review = subparsers.add_parser("review", help="list automatic captures awaiting review")
    _add_limit(review, default=100)

    update = subparsers.add_parser("update", help="update or accept a note")
    update.add_argument("note_id")
    update.add_argument("--title")
    update.add_argument("--body")
    update.add_argument("--label", action="append")
    update.add_argument("--importance", type=float)
    update.add_argument("--accept", action="store_true")
    update.add_argument("--sensitivity", choices=("normal", "sensitive"))
    update.add_argument("--confirm-sensitive", action="store_true")

    archive = subparsers.add_parser("archive", help="archive a note without deleting its source")
    archive.add_argument("note_id")

    delete = subparsers.add_parser("delete", help="permanently delete a note")
    delete.add_argument("note_id")
    delete.add_argument("--confirm", action="store_true")

    subparsers.add_parser("export", help="export notes and weighted graph as JSON")
    subparsers.add_parser("rebuild-index", help="rebuild SQLite FTS and graph index")
    return parser


def _execute(args: argparse.Namespace, store: NoteStore) -> dict[str, Any] | list[Any]:
    if args.command == "capture":
        return store.capture(
            title=args.title,
            body=args.body,
            labels=tuple(args.label),
            kind=args.kind,
            provenance=args.provenance,
            importance=args.importance,
            automatic=args.automatic,
            sensitivity=args.sensitivity,
            confirm_sensitive=args.confirm_sensitive,
        ).to_dict()
    if args.command == "show":
        return store.get(args.note_id).to_dict()
    if args.command == "search":
        return {
            "query": args.query,
            "labels": args.label,
            "related_to": args.related_to,
            "results": store.search(
                args.query,
                labels=tuple(args.label),
                kind=args.kind,
                related_to=args.related_to,
                limit=args.limit,
            ),
        }
    if args.command == "related":
        return {"note_id": args.note_id, "results": store.related(args.note_id, limit=args.limit)}
    if args.command == "link":
        return store.link(args.source_id, args.target_id).to_dict()
    if args.command == "review":
        return {"notes": [note.to_dict() for note in store.review(limit=args.limit)]}
    if args.command == "update":
        return store.update(
            args.note_id,
            title=args.title,
            body=args.body,
            labels=None if args.label is None else tuple(args.label),
            importance=args.importance,
            accept=args.accept,
            sensitivity=args.sensitivity,
            confirm_sensitive=args.confirm_sensitive,
        ).to_dict()
    if args.command == "archive":
        return store.archive(args.note_id).to_dict()
    if args.command == "delete":
        return store.delete(args.note_id, confirm=args.confirm)
    if args.command == "export":
        return store.export()
    if args.command == "rebuild-index":
        return store.rebuild_index()
    raise AssertionError(f"unhandled command: {args.command}")


def run_cli(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    store = NoteStore(root or default_notes_root())
    try:
        result = _execute(args, store)
    except SensitiveContentError as error:
        json.dump(
            {
                "error": str(error),
                "type": "privacy",
                "category": error.category,
                "storable": error.storable,
            },
            stderr,
            ensure_ascii=False,
        )
        stderr.write("\n")
        return 3
    except (KeyError, ValueError) as error:
        message = str(error.args[0]) if isinstance(error, KeyError) else str(error)
        json.dump({"error": message, "type": "input"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 1
    except (OSError, sqlite3.DatabaseError) as error:
        json.dump({"error": str(error), "type": "backend"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 2

    json.dump(result, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
