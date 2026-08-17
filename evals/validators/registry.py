from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .base import ValidationContext, ValidatorResult
from . import builtins
try:
    from ..redaction import redact_value
except ImportError:  # Direct imports with evals on sys.path.
    from redaction import redact_value


Validator = Callable[[Mapping[str, Any], ValidationContext, str], ValidatorResult]


class ValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, Validator] = {}

    def register(self, name: str, validator: Validator) -> None:
        if not name or name in self._validators:
            raise ValueError(f"validator already registered or invalid: {name}")
        self._validators[name] = validator

    def validate(self, spec: Mapping[str, Any], context: ValidationContext, index: int = 0) -> ValidatorResult:
        kind = str(spec.get("type", "")).strip()
        validator_id = str(spec.get("id", f"{kind or 'unknown'}:{index}"))
        validator = self._validators.get(kind)
        if validator is None:
            return ValidatorResult("unsupported", kind or "unknown", validator_id, (f"VALIDATOR_UNSUPPORTED:{kind or 'unknown'}",))
        try:
            return validator(spec, context, validator_id)
        except (OSError, UnicodeError, ValueError) as exc:
            return ValidatorResult("fail", kind, validator_id, (f"VALIDATOR_ERROR:{type(exc).__name__}:{exc}",))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._validators))


def default_registry() -> ValidatorRegistry:
    registry = ValidatorRegistry()
    for name, validator in {
        "path_scope": builtins.path_scope,
        "files_exist": builtins.files_exist,
        "json_exact": builtins.json_exact,
        "json_schema": builtins.json_schema,
        "markdown_headings": builtins.markdown_headings,
        "python_compile": builtins.python_compile,
        "forbidden_imports": builtins.forbidden_imports,
        "forbidden_pattern": builtins.forbidden_pattern,
        "command": builtins.command,
        "javascript_syntax": builtins.javascript_syntax,
        "typescript_check": builtins.typescript_check,
        "yaml_parse": builtins.yaml_parse,
    }.items():
        registry.register(name, validator)
    return registry


def validate_workspace_contract(
    workspace: Any,
    allowed_paths: Iterable[str],
    specs: Iterable[Mapping[str, Any]],
    *,
    changed_paths: Iterable[str] | None = None,
    allowed_commands: Iterable[Iterable[str]] | None = None,
    registry: ValidatorRegistry | None = None,
) -> list[dict[str, Any]]:
    from pathlib import Path

    context_kwargs: dict[str, Any] = {
        "workspace": Path(workspace),
        "allowed_paths": tuple(allowed_paths),
        "changed_paths": tuple(changed_paths) if changed_paths is not None else None,
    }
    if allowed_commands is not None:
        context_kwargs["allowed_commands"] = tuple(tuple(command) for command in allowed_commands)
    context = ValidationContext(**context_kwargs)
    active = registry or default_registry()
    all_specs = [
        {"id": "path_scope", "type": "path_scope", "allowed_paths": list(allowed_paths), "required": True},
        *list(specs),
    ]
    results: list[dict[str, Any]] = []
    for index, spec in enumerate(all_specs):
        result = redact_value(active.validate(spec, context, index).to_dict())
        result["required"] = bool(spec.get("required", True))
        results.append(result)
    return results
