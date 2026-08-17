from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ValidationContext:
    workspace: Path
    allowed_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] | None = None
    allowed_commands: tuple[tuple[str, ...], ...] = (
        ("python", "-m", "unittest"),
        ("python", "-m", "pytest"),
        ("node", "--check"),
        ("tsc", "--noEmit"),
        ("npm", "test"),
    )
    timeout_seconds: int = 120
    max_output_chars: int = 4000


@dataclass(frozen=True)
class ValidatorResult:
    status: str
    validator: str
    validator_id: str
    errors: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.validator_id,
            "type": self.validator,
            "status": self.status,
            "errors": list(self.errors),
            "details": dict(self.details),
        }
