"""Portable Markdown storage and an explainable SQLite graph index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import tomllib
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
INDEX_FILENAME = ".alfred-index.sqlite3"
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 500_000
MAX_LABELS = 32
VALID_KINDS = {"note", "preference", "fact", "decision", "idea", "procedure"}
VALID_SENSITIVITY = {"normal", "sensitive"}
VALID_STATUS = {"active", "archived"}
VALID_REVIEW_STATUS = {"accepted", "pending"}
WIKILINK_PATTERN = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
WORD_PATTERN = re.compile(r"[\w-]+", re.UNICODE)
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)
SENSITIVE_PERSONAL_PATTERNS = (
    re.compile(r"\bmedical diagnosis\b", re.IGNORECASE),
    re.compile(r"\bpassport number\b", re.IGNORECASE),
    re.compile(r"\bbank account\b", re.IGNORECASE),
    re.compile(r"\bsocial security number\b", re.IGNORECASE),
    re.compile(r"\bnational identity number\b", re.IGNORECASE),
)


class SensitiveContentError(ValueError):
    """Raised when a note needs user confirmation or belongs in a secret store."""

    def __init__(self, message: str, *, category: str, storable: bool):
        super().__init__(message)
        self.category = category
        self.storable = storable


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    title: str
    body: str
    labels: tuple[str, ...]
    kind: str
    provenance: str
    importance: float
    sensitivity: str
    status: str
    review_status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["labels"] = list(self.labels)
        return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _normalize_labels(labels: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in labels:
        label = re.sub(r"\s+", "-", str(value).strip().lower())
        if not label:
            continue
        if len(label) > 80:
            raise ValueError("labels must contain at most 80 characters")
        if label not in normalized:
            normalized.append(label)
    if len(normalized) > MAX_LABELS:
        raise ValueError(f"a note may contain at most {MAX_LABELS} labels")
    return tuple(sorted(normalized))


def _validate_note(note: Note) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(note.id):
        raise ValueError("note ID is invalid")
    if not note.title.strip() or len(note.title) > MAX_TITLE_CHARS:
        raise ValueError(f"title must contain between 1 and {MAX_TITLE_CHARS} characters")
    if len(note.body) > MAX_BODY_CHARS:
        raise ValueError(f"body must contain at most {MAX_BODY_CHARS} characters")
    if note.kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_KINDS))}")
    if note.sensitivity not in VALID_SENSITIVITY:
        raise ValueError("sensitivity must be normal or sensitive")
    if note.status not in VALID_STATUS:
        raise ValueError("status must be active or archived")
    if note.review_status not in VALID_REVIEW_STATUS:
        raise ValueError("review status must be accepted or pending")
    if not 0 <= note.importance <= 1:
        raise ValueError("importance must be between 0 and 1")
    _normalize_labels(note.labels)
    try:
        datetime.fromisoformat(note.created_at)
        datetime.fromisoformat(note.updated_at)
    except ValueError as error:
        raise ValueError("note timestamps must be ISO 8601 values") from error


def _check_sensitivity(
    title: str,
    body: str,
    *,
    sensitivity: str,
    confirm_sensitive: bool,
) -> None:
    content = f"{title}\n{body}"
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise SensitiveContentError(
            "possible credential detected; store credentials in a password manager, not notes",
            category="credential",
            storable=False,
        )
    detected_sensitive = any(pattern.search(content) for pattern in SENSITIVE_PERSONAL_PATTERNS)
    if detected_sensitive and (sensitivity != "sensitive" or not confirm_sensitive):
        raise SensitiveContentError(
            "sensitive personal content requires explicit confirmation",
            category="sensitive-personal",
            storable=True,
        )
    if sensitivity == "sensitive" and not confirm_sensitive:
        raise SensitiveContentError(
            "sensitive notes require explicit confirmation",
            category="sensitive-personal",
            storable=True,
        )


def _serialize_note(note: Note) -> str:
    label_values = ", ".join(_toml_string(label) for label in note.labels)
    fields = (
        f"id = {_toml_string(note.id)}",
        f"title = {_toml_string(note.title)}",
        f"labels = [{label_values}]",
        f"kind = {_toml_string(note.kind)}",
        f"provenance = {_toml_string(note.provenance)}",
        f"importance = {note.importance}",
        f"sensitivity = {_toml_string(note.sensitivity)}",
        f"status = {_toml_string(note.status)}",
        f"review_status = {_toml_string(note.review_status)}",
        f"created_at = {_toml_string(note.created_at)}",
        f"updated_at = {_toml_string(note.updated_at)}",
    )
    body = note.body.rstrip()
    return f"+++\n{chr(10).join(fields)}\n+++\n\n{body}\n"


def _parse_note(path: Path) -> Note:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("+++\n"):
        raise ValueError(f"{path.name}: missing TOML front matter")
    try:
        front_matter, body = content[4:].split("\n+++\n", 1)
    except ValueError as error:
        raise ValueError(f"{path.name}: unterminated TOML front matter") from error
    try:
        metadata = tomllib.loads(front_matter)
        note = Note(
            id=str(metadata["id"]),
            title=str(metadata["title"]).strip(),
            body=body.lstrip("\n").rstrip(),
            labels=_normalize_labels(tuple(str(value) for value in metadata.get("labels", []))),
            kind=str(metadata.get("kind", "note")),
            provenance=str(metadata.get("provenance", "user-stated")),
            importance=float(metadata.get("importance", 0.5)),
            sensitivity=str(metadata.get("sensitivity", "normal")),
            status=str(metadata.get("status", "active")),
            review_status=str(metadata.get("review_status", "accepted")),
            created_at=str(metadata["created_at"]),
            updated_at=str(metadata["updated_at"]),
        )
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path.name}: invalid note metadata: {error}") from error
    _validate_note(note)
    if path.stem != note.id:
        raise ValueError(f"{path.name}: filename must match note ID")
    return note


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class NoteStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.index_path = self.root / INDEX_FILENAME

    def capture(
        self,
        *,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        kind: str = "note",
        provenance: str | None = None,
        importance: float = 0.5,
        sensitivity: str = "normal",
        confirm_sensitive: bool = False,
        automatic: bool = False,
    ) -> Note:
        title = title.strip()
        body = body.strip()
        timestamp = _now()
        note = Note(
            id=str(uuid.uuid4()),
            title=title,
            body=body,
            labels=_normalize_labels(labels),
            kind=kind,
            provenance=provenance or ("alfred-inferred" if automatic else "user-stated"),
            importance=float(importance),
            sensitivity=sensitivity,
            status="active",
            review_status="pending" if automatic else "accepted",
            created_at=timestamp,
            updated_at=timestamp,
        )
        _validate_note(note)
        _check_sensitivity(
            note.title,
            note.body,
            sensitivity=note.sensitivity,
            confirm_sensitive=confirm_sensitive,
        )
        self._write(note)
        self.rebuild_index()
        return note

    def get(self, note_id: str) -> Note:
        return _parse_note(self._path(note_id))

    def update(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: tuple[str, ...] | None = None,
        importance: float | None = None,
        accept: bool = False,
        sensitivity: str | None = None,
        confirm_sensitive: bool = False,
    ) -> Note:
        current = self.get(note_id)
        updated = replace(
            current,
            title=current.title if title is None else title.strip(),
            body=current.body if body is None else body.strip(),
            labels=current.labels if labels is None else _normalize_labels(labels),
            importance=current.importance if importance is None else float(importance),
            sensitivity=current.sensitivity if sensitivity is None else sensitivity,
            review_status="accepted" if accept else current.review_status,
            updated_at=_now(),
        )
        _validate_note(updated)
        _check_sensitivity(
            updated.title,
            updated.body,
            sensitivity=updated.sensitivity,
            confirm_sensitive=confirm_sensitive
            or updated.sensitivity == current.sensitivity == "sensitive",
        )
        self._write(updated)
        self.rebuild_index()
        return updated

    def archive(self, note_id: str) -> Note:
        note = replace(self.get(note_id), status="archived", updated_at=_now())
        self._write(note)
        self.rebuild_index()
        return note

    def delete(self, note_id: str, *, confirm: bool = False) -> dict[str, str]:
        if not confirm:
            raise ValueError("permanent deletion requires explicit confirmation")
        path = self._path(note_id)
        path.unlink()
        self.rebuild_index()
        return {"deleted": note_id}

    def review(self, *, limit: int = 100) -> list[Note]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        return [
            note
            for note in self._load_notes()
            if note.status == "active" and note.review_status == "pending"
        ][:limit]

    def link(self, source_id: str, target_id: str) -> Note:
        source = self.get(source_id)
        target = self.get(target_id)
        wikilink = f"[[{target.id}|{target.title}]]"
        if wikilink in source.body:
            return source
        separator = "\n\n" if source.body else ""
        return self.update(source.id, body=f"{source.body}{separator}Related: {wikilink}")

    def search(
        self,
        query: str,
        *,
        labels: tuple[str, ...] = (),
        kind: str | None = None,
        related_to: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) > 500:
            raise ValueError("query must contain at most 500 characters")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if kind is not None and kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_KINDS))}")
        wanted_labels = set(_normalize_labels(labels))
        self._ensure_index()
        with closing(sqlite3.connect(self.index_path)) as connection:
            connection.row_factory = sqlite3.Row
            if query:
                terms = WORD_PATTERN.findall(query)
                if not terms:
                    return []
                match = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
                rows = connection.execute(
                    "SELECT n.*, bm25(notes_fts, 0.0, 5.0, 1.0, 1.0) AS text_rank "
                    "FROM notes_fts JOIN notes n ON n.id = notes_fts.id "
                    "WHERE notes_fts MATCH ? AND n.status = 'active' "
                    "ORDER BY text_rank LIMIT 500",
                    (match,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT n.*, NULL AS text_rank FROM notes n WHERE n.status = 'active' LIMIT 500"
                ).fetchall()

            results: list[dict[str, Any]] = []
            for position, row in enumerate(rows):
                note = self._row_note(row)
                note_labels = set(note.labels)
                if wanted_labels and not wanted_labels.issubset(note_labels):
                    continue
                if kind is not None and note.kind != kind:
                    continue
                graph = self._graph_score(connection, note.id, related_to) if related_to else 0.0
                label_hits = (
                    len(wanted_labels)
                    if wanted_labels
                    else len(
                        note_labels.intersection(
                            term.lower() for term in WORD_PATTERN.findall(query)
                        )
                    )
                )
                components = {
                    "text": round(1 / (position + 1), 6) if query else 0.0,
                    "labels": round(min(0.4, label_hits * 0.2), 6),
                    "graph": round(graph * 0.5, 6),
                    "importance": round(note.importance * 0.25, 6),
                    "recency": round(self._recency(note.updated_at, maximum=0.15), 6),
                }
                results.append(
                    {
                        "note": note.to_dict(),
                        "score": round(sum(components.values()), 6),
                        "score_components": components,
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["note"]["title"].casefold()))
        return results[:limit]

    def related(self, note_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self.get(note_id)
        self._ensure_index()
        with closing(sqlite3.connect(self.index_path)) as connection:
            connection.row_factory = sqlite3.Row
            edges = connection.execute(
                "SELECT source_id, target_id, weight, origin FROM edges "
                "WHERE origin = 'explicit' AND (source_id = ? OR target_id = ?)",
                (note_id, note_id),
            ).fetchall()
            components_by_id: dict[str, dict[str, float]] = {}
            for edge in edges:
                other = edge["target_id"] if edge["source_id"] == note_id else edge["source_id"]
                components = components_by_id.setdefault(
                    other,
                    {"explicit": 0.0, "shared_labels": 0.0},
                )
                components["explicit"] += float(edge["weight"])
            shared_rows = connection.execute(
                "SELECT other.source_id AS related_id, MIN(own.weight, other.weight) AS weight "
                "FROM edges own JOIN edges other ON other.target_id = own.target_id "
                "WHERE own.source_id = ? AND own.origin = 'inferred' "
                "AND other.origin = 'inferred' AND other.source_id != ?",
                (note_id, note_id),
            ).fetchall()
            for edge in shared_rows:
                components = components_by_id.setdefault(
                    edge["related_id"],
                    {"explicit": 0.0, "shared_labels": 0.0},
                )
                components["shared_labels"] = min(
                    0.6, components["shared_labels"] + float(edge["weight"])
                )

            results: list[dict[str, Any]] = []
            for related_id, edge_components in components_by_id.items():
                row = connection.execute(
                    "SELECT * FROM notes WHERE id = ? AND status = 'active'", (related_id,)
                ).fetchone()
                if row is None:
                    continue
                note = self._row_note(row)
                components = {
                    "explicit": round(edge_components["explicit"], 6),
                    "shared_labels": round(edge_components["shared_labels"], 6),
                    "importance": round(note.importance * 0.15, 6),
                    "recency": round(self._recency(note.updated_at, maximum=0.1), 6),
                }
                results.append(
                    {
                        "note": note.to_dict(),
                        "score": round(sum(components.values()), 6),
                        "score_components": components,
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["note"]["title"].casefold()))
        return results[:limit]

    def export(self) -> dict[str, Any]:
        self._ensure_index()
        notes = [note.to_dict() for note in self._load_notes()]
        with closing(sqlite3.connect(self.index_path)) as connection:
            connection.row_factory = sqlite3.Row
            edges = [
                dict(row)
                for row in connection.execute(
                    "SELECT source_id, target_id, relation, weight, origin, evidence "
                    "FROM edges ORDER BY source_id, target_id, origin"
                )
            ]
            nodes = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, node_type, label FROM nodes ORDER BY node_type, id"
                )
            ]
        for edge in edges:
            edge["evidence"] = json.loads(edge["evidence"])
        return {
            "schema_version": SCHEMA_VERSION,
            "notes": notes,
            "nodes": nodes,
            "edges": edges,
        }

    def rebuild_index(self) -> dict[str, int]:
        self._ensure_root()
        paths = self._note_paths()
        notes = [_parse_note(path) for path in paths]
        temporary_path = self.index_path.with_name(f"{self.index_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with closing(sqlite3.connect(temporary_path)) as connection:
                self._create_schema(connection)
                for note in notes:
                    labels = json.dumps(note.labels, ensure_ascii=False)
                    connection.execute(
                        "INSERT INTO nodes (id, node_type, label) VALUES (?, 'note', ?)",
                        (note.id, note.title),
                    )
                    connection.execute(
                        "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            note.id,
                            note.title,
                            note.body,
                            labels,
                            note.kind,
                            note.provenance,
                            note.importance,
                            note.sensitivity,
                            note.status,
                            note.review_status,
                            note.created_at,
                            note.updated_at,
                            f"{note.id}.md",
                        ),
                    )
                    if note.status == "active":
                        connection.execute(
                            "INSERT INTO notes_fts (id, title, body, labels) VALUES (?, ?, ?, ?)",
                            (note.id, note.title, note.body, " ".join(note.labels)),
                        )
                edge_count = self._index_edges(connection, notes)
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('source_fingerprint', ?)",
                    (_fingerprint(paths),),
                )
                connection.commit()
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.index_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return {"notes": len(notes), "edges": edge_count}

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, note_id: str) -> Path:
        if not IDENTIFIER_PATTERN.fullmatch(note_id):
            raise ValueError("note ID is invalid")
        path = self.root / f"{note_id}.md"
        if not path.is_file():
            raise KeyError(f"note not found: {note_id}")
        return path

    def _write(self, note: Note) -> None:
        self._ensure_root()
        path = self.root / f"{note.id}.md"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{note.id}.", dir=self.root)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(_serialize_note(note))
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _note_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            path
            for path in self.root.glob("*.md")
            if path.is_file() and not path.name.startswith(".")
        )

    def _load_notes(self) -> list[Note]:
        return [_parse_note(path) for path in self._note_paths()]

    def _ensure_index(self) -> None:
        paths = self._note_paths()
        if not self.index_path.exists():
            self.rebuild_index()
            return
        try:
            with closing(sqlite3.connect(self.index_path)) as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'source_fingerprint'"
                ).fetchone()
        except sqlite3.DatabaseError:
            self.rebuild_index()
            return
        if row is None or row[0] != _fingerprint(paths):
            self.rebuild_index()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                labels TEXT NOT NULL,
                kind TEXT NOT NULL,
                provenance TEXT NOT NULL,
                importance REAL NOT NULL,
                sensitivity TEXT NOT NULL,
                status TEXT NOT NULL,
                review_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                path TEXT NOT NULL
            );
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                label TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                id UNINDEXED,
                title,
                body,
                labels,
                tokenize = 'unicode61'
            );
            CREATE TABLE edges (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL,
                origin TEXT NOT NULL,
                evidence TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, relation, origin)
            );
            CREATE INDEX edges_source ON edges(source_id);
            CREATE INDEX edges_target ON edges(target_id);
            """
        )

    @staticmethod
    def _index_edges(connection: sqlite3.Connection, notes: list[Note]) -> int:
        active = [note for note in notes if note.status == "active"]
        by_id = {note.id: note for note in active}
        title_groups: dict[str, list[Note]] = {}
        for note in active:
            title_groups.setdefault(note.title.casefold(), []).append(note)
        by_unique_title = {
            title: matches[0] for title, matches in title_groups.items() if len(matches) == 1
        }
        edges: list[tuple[str, str, str, float, str, str]] = []
        for note in active:
            for target_reference in WIKILINK_PATTERN.findall(note.body):
                target = by_id.get(target_reference.strip()) or by_unique_title.get(
                    target_reference.strip().casefold()
                )
                if target is None or target.id == note.id:
                    continue
                edges.append(
                    (
                        note.id,
                        target.id,
                        "references",
                        1.0,
                        "explicit",
                        json.dumps({"wikilink": target_reference.strip()}, ensure_ascii=False),
                    )
                )
        for note in active:
            for label in note.labels:
                label_id = f"label:{label}"
                connection.execute(
                    "INSERT OR IGNORE INTO nodes (id, node_type, label) VALUES (?, 'label', ?)",
                    (label_id, label),
                )
                edges.append(
                    (
                        note.id,
                        label_id,
                        "has-label",
                        0.15,
                        "inferred",
                        json.dumps({"label": label}, ensure_ascii=False),
                    )
                )
        connection.executemany("INSERT OR REPLACE INTO edges VALUES (?, ?, ?, ?, ?, ?)", edges)
        return len(edges)

    @staticmethod
    def _row_note(row: sqlite3.Row) -> Note:
        return Note(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            labels=tuple(json.loads(row["labels"])),
            kind=row["kind"],
            provenance=row["provenance"],
            importance=float(row["importance"]),
            sensitivity=row["sensitivity"],
            status=row["status"],
            review_status=row["review_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _graph_score(connection: sqlite3.Connection, note_id: str, related_to: str | None) -> float:
        if related_to is None or note_id == related_to:
            return 0.0
        direct_row = connection.execute(
            "SELECT COALESCE(SUM(weight), 0) FROM edges WHERE origin = 'explicit' AND "
            "((source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?))",
            (note_id, related_to, related_to, note_id),
        ).fetchone()
        shared_row = connection.execute(
            "SELECT COUNT(*) FROM edges first "
            "JOIN edges second ON second.target_id = first.target_id "
            "WHERE first.source_id = ? AND second.source_id = ? "
            "AND first.origin = 'inferred' AND second.origin = 'inferred'",
            (note_id, related_to),
        ).fetchone()
        direct = float(direct_row[0]) if direct_row else 0.0
        shared = min(0.6, (int(shared_row[0]) if shared_row else 0) * 0.15)
        return direct + shared

    @staticmethod
    def _recency(value: str, *, maximum: float) -> float:
        updated = datetime.fromisoformat(value)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - updated).total_seconds() / 86_400)
        return maximum * math.exp(-age_days / 180)
