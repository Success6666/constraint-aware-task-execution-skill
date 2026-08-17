"""Ollama HTTP execution backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .base import (
    ExecutorCapabilities,
    FailureKind,
    GenerationExecutor,
    GenerationRequest,
    GenerationResult,
    Usage,
    classify_failure,
)
try:
    from ..redaction import atomic_write_text, redact_text, redact_value
except ImportError:  # Direct execution from the evals directory.
    from redaction import atomic_write_text, redact_text, redact_value


DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


def _ollama_endpoint(environment: Mapping[str, str]) -> str:
    raw_host = environment.get("OLLAMA_HOST") or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST
    host = raw_host.strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    parsed = urllib_parse.urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OLLAMA_HOST must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OLLAMA_HOST must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("OLLAMA_HOST must not contain a path")
    return urllib_parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/generate", "", ""))


def _load_schema(value: Mapping[str, Any] | Path | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Path):
        parsed = json.loads(value.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("output schema must be a JSON object")
        return parsed
    return dict(value)


class OllamaCliExecutor(GenerationExecutor):
    """Compatibility name for the Ollama HTTP backend used by existing configs."""

    name = "ollama"
    default_model = "qwen3.5:9b"
    capabilities = ExecutorCapabilities(
        structured_output=True,
        workspace_access=False,
        reasoning_effort=False,
        usage_reporting=True,
        local_execution=True,
    )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.monotonic()
        model = request.model or self.default_model
        if not request.cwd.is_dir():
            return self._failure(
                request,
                model,
                started,
                FailureKind.INVALID_REQUEST,
                f"workspace directory does not exist: {request.cwd}",
            )

        try:
            endpoint = _ollama_endpoint(request.environment)
            schema = _load_schema(request.output_schema)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            return self._failure(
                request,
                model,
                started,
                FailureKind.INVALID_REQUEST,
                f"invalid Ollama request: {error}",
            )

        max_output_tokens = request.metadata.get("max_output_tokens", 4096)
        if not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool):
            max_output_tokens = 4096
        max_output_tokens = min(max(max_output_tokens, 1), 32768)
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.full_prompt(),
            "stream": False,
            "think": False,
            "options": {"num_predict": max_output_tokens},
        }
        if schema is not None:
            payload["format"] = schema

        http_request = urllib_request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except OSError:
                detail = str(error)
            return self._failure(
                request,
                model,
                started,
                classify_failure(detail, returncode=error.code),
                detail or str(error),
                returncode=error.code,
            )
        except (TimeoutError, socket.timeout) as error:
            return self._failure(
                request,
                model,
                started,
                FailureKind.TIMEOUT,
                str(error) or "Ollama request timed out",
            )
        except (urllib_error.URLError, OSError) as error:
            detail = str(getattr(error, "reason", error))
            return self._failure(
                request,
                model,
                started,
                classify_failure(detail),
                detail,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return self._failure(
                request,
                model,
                started,
                FailureKind.OUTPUT_ERROR,
                f"malformed Ollama response: {error}",
            )

        if not isinstance(response_payload, dict):
            return self._failure(
                request,
                model,
                started,
                FailureKind.OUTPUT_ERROR,
                "malformed Ollama response: expected an object",
            )
        output = response_payload.get("response")
        if not isinstance(output, str) or not output.strip():
            return self._failure(
                request,
                model,
                started,
                FailureKind.EMPTY_OUTPUT,
                "empty model output",
            )

        safe_output = redact_text(output)
        trace_payload = {
            "model": response_payload.get("model", model),
            "done": response_payload.get("done"),
            "done_reason": response_payload.get("done_reason"),
            "total_duration_ns": response_payload.get("total_duration"),
            "load_duration_ns": response_payload.get("load_duration"),
            "prompt_eval_count": response_payload.get("prompt_eval_count"),
            "eval_count": response_payload.get("eval_count"),
        }
        if request.trace_path:
            atomic_write_text(
                request.trace_path,
                json.dumps(redact_value(trace_payload), ensure_ascii=False, sort_keys=True) + "\n",
            )
        if request.output_path:
            atomic_write_text(request.output_path, safe_output)
        prompt_tokens = response_payload.get("prompt_eval_count")
        output_tokens = response_payload.get("eval_count")
        usage = Usage(
            input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            raw={
                key: response_payload[key]
                for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
                if isinstance(response_payload.get(key), int)
            },
        )
        return GenerationResult(
            executor=self.name,
            model=model,
            success=True,
            output=safe_output,
            returncode=0,
            duration_seconds=time.monotonic() - started,
            usage=usage,
            capabilities=self.capabilities,
            trace_path=request.trace_path,
            output_path=request.output_path,
            metadata=request.metadata,
        )

    def _failure(
        self,
        request: GenerationRequest,
        model: str,
        started: float,
        kind: FailureKind,
        message: str,
        *,
        returncode: int | None = None,
    ) -> GenerationResult:
        safe_message = redact_text(message)
        if request.trace_path:
            atomic_write_text(request.trace_path, safe_message + "\n")
        if request.output_path:
            atomic_write_text(request.output_path, "")
        return GenerationResult(
            executor=self.name,
            model=model,
            success=False,
            returncode=returncode,
            duration_seconds=time.monotonic() - started,
            failure_kind=kind,
            failure_message=safe_message,
            capabilities=self.capabilities,
            trace_path=request.trace_path,
            output_path=request.output_path,
            metadata=request.metadata,
        )
