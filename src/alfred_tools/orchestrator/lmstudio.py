"""Deterministic lifecycle management for local LM Studio models."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from alfred_tools.orchestrator.models import ModelBackendError


class LMStudioManager:
    def __init__(
        self,
        *,
        base_url: str,
        configured_model: str | None = None,
        loaded_models: Callable[[], list[str]] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        ttl_seconds: int = 1800,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("LM Studio must use loopback HTTP")
        if ttl_seconds < 60:
            raise ValueError("LM Studio TTL must be at least 60 seconds")
        self.base_url = base_url.rstrip("/")
        self.configured_model = configured_model.strip() if configured_model else None
        self.loaded_models = loaded_models or self._loaded_models_api
        self.runner = runner
        self.ttl_seconds = ttl_seconds
        self.port = parsed.port or 1234
        self._lock = threading.Lock()

    def ensure_model(self) -> str:
        with self._lock:
            try:
                loaded = self.loaded_models()
            except ModelBackendError:
                self._run(
                    [
                        "lms",
                        "server",
                        "start",
                        "--port",
                        str(self.port),
                        "--bind",
                        "127.0.0.1",
                    ],
                    timeout=20,
                )
                loaded = self.loaded_models()
            if self.configured_model and self.configured_model in loaded:
                return self.configured_model
            if not self.configured_model and loaded:
                return sorted(loaded)[0]

            installed = self._installed_models()
            selected = self._select(installed)
            self._run(
                [
                    "lms",
                    "load",
                    selected,
                    "--identifier",
                    "alfred-local",
                    "--gpu",
                    "max",
                    "--ttl",
                    str(self.ttl_seconds),
                    "--yes",
                ],
                timeout=300,
            )
            return "alfred-local"

    def _loaded_models_api(self) -> list[str]:
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                data = response.read(1_000_001)
        except (OSError, urllib.error.URLError) as error:
            raise ModelBackendError(f"could not reach LM Studio: {error}") from error
        if len(data) > 1_000_000:
            raise ModelBackendError("LM Studio model list exceeded 1 MB")
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelBackendError("LM Studio returned an invalid model list") from error
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return sorted(
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )

    def _installed_models(self) -> list[dict[str, Any]]:
        result = self._run(["lms", "ls", "--llm", "--json"], timeout=30)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ModelBackendError("lms returned an invalid installed-model list") from error
        if not isinstance(value, list):
            raise ModelBackendError("lms installed-model list was not an array")
        return [item for item in value if isinstance(item, dict)]

    def _select(self, installed: list[dict[str, Any]]) -> str:
        if self.configured_model:
            for item in installed:
                identifiers = {
                    item.get("modelKey"),
                    item.get("selectedVariant"),
                    item.get("indexedModelIdentifier"),
                }
                if self.configured_model in identifiers:
                    return self.configured_model
            raise ModelBackendError(
                f"configured LM Studio model is not installed: {self.configured_model}"
            )
        candidates = [item for item in installed if item.get("trainedForToolUse") is True]
        if not candidates:
            raise ModelBackendError(
                "no installed tool-capable LM Studio model was found; configure "
                "ALFRED_LMSTUDIO_MODEL explicitly"
            )
        chosen = min(
            candidates,
            key=lambda item: (int(item.get("sizeBytes") or 2**63), str(item.get("modelKey") or "")),
        )
        model_key = chosen.get("modelKey")
        if not isinstance(model_key, str) or not model_key:
            raise ModelBackendError("selected LM Studio model has no model key")
        return model_key

    def _run(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelBackendError(f"could not run {' '.join(command[:2])}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise ModelBackendError(f"{' '.join(command[:2])} failed{suffix}")
        return result
