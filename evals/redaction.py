"""Redaction helpers for persisted model output and execution traces."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


REDACTED = "[REDACTED]"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(https?://[^\s/:@]+:)[^\s/@]+(@)"), rf"\1{REDACTED}\2"),
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s\"']+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|session[_-]?token|cookie)\s*[=:]\s*)[^\s,;\"']+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)(\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)\"\s*:\s*\")[^\"]*(\")"), rf"\1{REDACTED}\2"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), REDACTED),
    (re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"), REDACTED),
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    home = str(Path.home())
    if home:
        redacted = redacted.replace(home, "~").replace(home.replace("\\", "/"), "~")
    redacted = re.sub(r"codex-eval-[A-Za-z0-9_-]+", f"codex-eval-{REDACTED}", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if re.search(r"(?i)(?:password|passwd|secret|token|api[_-]?key|cookie|authorization)", str(key)):
                result[key] = REDACTED
            else:
                result[key] = redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(redact_text(content))
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    content = json.dumps(redact_value(value), ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(path, content + "\n")
