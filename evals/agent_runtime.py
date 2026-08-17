"""Stable JSON command-line interface for one model generation request."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
from typing import Any

try:
    from .executors import GenerationRequest, create_executor
    from .redaction import atomic_write_json, redact_value
    from .runtime_env import ALLOWED_ENVIRONMENT_OVERRIDES
except ImportError:  # Supports `python evals/agent_runtime.py`.
    from executors import GenerationRequest, create_executor
    from redaction import atomic_write_json, redact_value
    from runtime_env import ALLOWED_ENVIRONMENT_OVERRIDES


PROTOCOL_VERSION = "constraint-exec-generation/v1"
SUPPORTED_EXECUTORS = ("codex", "ollama")
SUPPORTED_SANDBOXES = ("read-only", "workspace-write")


class RequestError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute one JSON model generation request.")
    parser.add_argument(
        "--request",
        default="-",
        help="JSON request file, or - to read stdin.",
    )
    parser.add_argument("--response", type=Path, help="Optional JSON response file.")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--describe", action="store_true", help="Print protocol capabilities.")
    return parser.parse_args()


def describe_protocol() -> dict[str, Any]:
    executors = {}
    for name in SUPPORTED_EXECUTORS:
        backend = create_executor(name)
        executors[name] = {
            "name": backend.name,
            "capabilities": backend.capabilities.to_dict(),
            **({"default_model": backend.default_model} if hasattr(backend, "default_model") else {}),
        }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "capabilities",
        "executors": executors,
        "sandboxes": list(SUPPORTED_SANDBOXES),
    }


def load_payload(source: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise RequestError(f"invalid request JSON: {error}") from error
    if not isinstance(payload, dict):
        raise RequestError("request JSON must be an object")
    return payload


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{key} must be a non-empty string")
    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string")
    return value


def _path(value: Any, *, base: Path, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RequestError(f"{key} must be a non-empty path string")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _contained_artifact_path(
    payload: Mapping[str, Any],
    key: str,
    *,
    base: Path,
    artifact_root: Path,
) -> Path | None:
    value = payload.get(key)
    if value is None:
        return None
    path = _path(value, base=base, key=key)
    if path != artifact_root and artifact_root not in path.parents:
        raise RequestError(f"{key} must stay within artifact_root")
    return path


def build_request(payload: Mapping[str, Any]) -> tuple[str, GenerationRequest]:
    executor_name = str(payload.get("executor", "codex")).strip().casefold()
    if executor_name not in SUPPORTED_EXECUTORS:
        raise RequestError(f"executor must be one of: {', '.join(SUPPORTED_EXECUTORS)}")

    base = Path.cwd().resolve()
    workspace_root = _path(payload.get("workspace_root", str(base)), base=base, key="workspace_root")
    cwd_value = payload.get("cwd", str(base))
    cwd = _path(cwd_value, base=base, key="cwd")
    if cwd != workspace_root and workspace_root not in cwd.parents:
        raise RequestError("cwd must stay within workspace_root")
    if not cwd.is_dir():
        raise RequestError(f"cwd does not exist or is not a directory: {cwd}")

    artifact_root_value = payload.get("artifact_root", str(cwd))
    artifact_root = _path(artifact_root_value, base=base, key="artifact_root")
    artifact_root.mkdir(parents=True, exist_ok=True)

    model_value = payload.get("model")
    if model_value is None and executor_name == "ollama":
        model = "qwen3.5:9b"
    else:
        model = _required_text(payload, "model")

    timeout = payload.get("timeout_seconds", 300.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
        raise RequestError("timeout_seconds must be between 0 and 3600")

    sandbox = str(payload.get("sandbox", "read-only"))
    if sandbox not in SUPPORTED_SANDBOXES:
        raise RequestError(f"sandbox must be one of: {', '.join(SUPPORTED_SANDBOXES)}")

    environment = payload.get("environment", {})
    metadata = payload.get("metadata", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise RequestError("environment must be an object containing string values")
    rejected_environment = sorted(set(environment) - ALLOWED_ENVIRONMENT_OVERRIDES)
    if rejected_environment:
        raise RequestError(
            "environment contains unsupported overrides: " + ", ".join(rejected_environment)
        )
    if not isinstance(metadata, dict):
        raise RequestError("metadata must be an object")

    schema_value = payload.get("output_schema")
    output_schema: Mapping[str, Any] | Path | None
    if schema_value is None or isinstance(schema_value, dict):
        output_schema = schema_value
    elif isinstance(schema_value, str):
        output_schema = _path(schema_value, base=base, key="output_schema")
        if not output_schema.is_file():
            raise RequestError(f"output_schema does not exist: {output_schema}")
    else:
        raise RequestError("output_schema must be an object or path string")

    request = GenerationRequest(
        prompt=_required_text(payload, "prompt"),
        model=model,
        cwd=cwd,
        timeout_seconds=float(timeout),
        system_prompt=_optional_text(payload, "system_prompt"),
        reasoning_effort=_optional_text(payload, "reasoning_effort"),
        sandbox=sandbox,
        trace_path=_contained_artifact_path(
            payload, "trace_path", base=base, artifact_root=artifact_root
        ),
        output_path=_contained_artifact_path(
            payload, "output_path", base=base, artifact_root=artifact_root
        ),
        output_schema=output_schema,
        environment=environment,
        metadata=metadata,
    )
    return executor_name, request


def execute_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    operation = payload.get("operation", "generate")
    if operation == "capabilities":
        return describe_protocol(), 0
    if operation != "generate":
        raise RequestError("operation must be generate or capabilities")
    executor_name, request = build_request(payload)
    result = create_executor(executor_name).generate(request)
    response = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "generate",
        "result": result.to_dict(),
    }
    return redact_value(response), 0 if result.success else 1


def emit_response(payload: Mapping[str, Any], *, path: Path | None, pretty: bool) -> None:
    safe_payload = redact_value(dict(payload))
    if path is not None:
        atomic_write_json(path.resolve(), safe_payload)
        return
    print(
        json.dumps(
            safe_payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=pretty,
        )
    )


def main() -> int:
    args = parse_args()
    try:
        response, exit_code = (
            (describe_protocol(), 0)
            if args.describe
            else execute_payload(load_payload(args.request))
        )
    except RequestError as error:
        response = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "error",
            "error": {"kind": "invalid_request", "message": str(error)},
        }
        exit_code = 2
    emit_response(response, path=args.response, pretty=args.pretty)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
