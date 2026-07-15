"""Install a version-controlled Alfred skill into Codex's personal skill directory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def install_skill(source: Path, destination: Path) -> None:
    """Copy a valid skill tree to its discovery location."""
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"skill source must contain SKILL.md: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install a local Alfred skill for Codex")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    destination = args.destination
    if destination is None:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        destination = codex_home / "skills" / args.source.name
    try:
        install_skill(args.source.resolve(), destination.resolve())
    except (OSError, ValueError) as error:
        json.dump({"error": str(error)}, sys.stderr)
        sys.stderr.write("\n")
        return 1
    json.dump({"installed": str(destination.resolve())}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
