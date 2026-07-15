"""Small, non-executable local preference loader for Alfred."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ALLOWED_PREFERENCES = frozenset(
    {
        "ALFRED_TIME_ZONE",
        "ALFRED_WEATHER_LOCATION",
    }
)
MAX_PREFERENCES_BYTES = 16_384


def default_preferences_path() -> Path:
    configured = os.environ.get("ALFRED_PREFERENCES_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "alfred" / "preferences.env"


def load_preferences(path: Path | None = None) -> dict[str, str]:
    source = path or default_preferences_path()
    try:
        if source.stat().st_size > MAX_PREFERENCES_BYTES:
            raise ValueError("Alfred preferences file exceeds 16 KB")
        content = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(f"could not read Alfred preferences: {error}") from error

    preferences: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in ALLOWED_PREFERENCES:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        preferences[key] = value
    return preferences


def get_preference(
    name: str,
    default: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> str | None:
    if name not in ALLOWED_PREFERENCES:
        raise ValueError(f"unsupported Alfred preference: {name}")
    values = os.environ if environment is None else environment
    if name in values:
        return str(values[name]).strip()
    return load_preferences(path).get(name, default)
