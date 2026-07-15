"""Parse a deliberately small Markdown subset into an inert rendering tree."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from typing import Any

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_ITEM = re.compile(r"^\s*(?:(?P<number>\d+)\.|[-+*])\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*```(?P<language>[A-Za-z0-9_+-]{0,30})\s*$")
DIVIDER_CELL = re.compile(r"^:?-{2,}:?$")
INLINE = re.compile(
    r"`[^`\n]+`"
    r"|\*\*[^*\n]+\*\*"
    r"|__[^_\n]+__"
    r"|\[[^\]\n]+\]\([^\s)]+(?:\([^\s)]*\))?\)"
    r"|\*[^*\n]+\*"
    r"|_[^_\n]+_"
)


def _append_text(tokens: list[dict[str, Any]], text: str) -> None:
    if not text:
        return
    if tokens and tokens[-1]["type"] == "text":
        tokens[-1]["text"] += text
    else:
        tokens.append({"type": "text", "text": text})


def _safe_link(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        return True


def _inline(text: str, depth: int = 0) -> list[dict[str, Any]]:
    if depth >= 4:
        return [{"type": "text", "text": text}]
    tokens: list[dict[str, Any]] = []
    position = 0
    for match in INLINE.finditer(text):
        _append_text(tokens, text[position : match.start()])
        raw = match.group(0)
        if raw.startswith("`"):
            tokens.append({"type": "code", "text": raw[1:-1]})
        elif raw.startswith(("**", "__")):
            tokens.append({"type": "strong", "content": _inline(raw[2:-2], depth + 1)})
        elif raw.startswith("["):
            label, url = raw[1:].split("](", 1)
            url = url[:-1]
            if _safe_link(url):
                tokens.append({"type": "link", "content": _inline(label, depth + 1), "url": url})
            else:
                _append_text(tokens, raw)
        else:
            tokens.append({"type": "emphasis", "content": _inline(raw[1:-1], depth + 1)})
        position = match.end()
    _append_text(tokens, text[position:])
    return tokens or [{"type": "text", "text": ""}]


def _table_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _table_alignment(line: str, columns: int) -> list[str] | None:
    cells = _table_cells(line)
    if len(cells) != columns or not all(DIVIDER_CELL.fullmatch(cell) for cell in cells):
        return None
    return [
        "center"
        if cell.startswith(":") and cell.endswith(":")
        else "right"
        if cell.endswith(":")
        else "left"
        for cell in cells
    ]


def _starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    if (
        not line.strip()
        or HEADING.match(line)
        or LIST_ITEM.match(line)
        or FENCE.match(line)
        or line.lstrip().startswith(">")
        or re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line)
    ):
        return True
    if index + 1 < len(lines) and "|" in line:
        return _table_alignment(lines[index + 1], len(_table_cells(line))) is not None
    return False


def parse_markdown(markdown: str) -> list[dict[str, Any]]:
    """Return safe block and inline tokens; raw HTML remains ordinary text."""

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = FENCE.match(line)
        if fence:
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not re.match(r"^\s*```\s*$", lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "type": "code",
                    "language": fence.group("language"),
                    "text": "\n".join(code_lines),
                }
            )
            continue

        heading = HEADING.match(line)
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading.group(1)),
                    "content": _inline(heading.group(2)),
                }
            )
            index += 1
            continue

        headers = _table_cells(line)
        alignment = (
            _table_alignment(lines[index + 1], len(headers))
            if index + 1 < len(lines) and "|" in line
            else None
        )
        if alignment is not None:
            index += 2
            rows: list[list[list[dict[str, Any]]]] = []
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                cells = _table_cells(lines[index])[: len(headers)]
                cells.extend([""] * (len(headers) - len(cells)))
                rows.append([_inline(cell) for cell in cells])
                index += 1
            blocks.append(
                {
                    "type": "table",
                    "align": alignment,
                    "headers": [_inline(cell) for cell in headers],
                    "rows": rows,
                }
            )
            continue

        list_item = LIST_ITEM.match(line)
        if list_item:
            ordered = list_item.group("number") is not None
            items: list[list[dict[str, str]]] = []
            while index < len(lines):
                item = LIST_ITEM.match(lines[index])
                if item is None or (item.group("number") is not None) != ordered:
                    break
                items.append(_inline(item.group(2)))
                index += 1
            blocks.append({"type": "list", "ordered": ordered, "items": items})
            continue

        if line.lstrip().startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote_lines.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            blocks.append({"type": "quote", "content": _inline(" ".join(quote_lines))})
            continue

        if re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line):
            blocks.append({"type": "rule"})
            index += 1
            continue

        paragraph = [line.strip()]
        index += 1
        while index < len(lines) and not _starts_block(lines, index):
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append({"type": "paragraph", "content": _inline(" ".join(paragraph))})

    return blocks
