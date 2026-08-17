"""Shared request, response, and failure contracts for model executors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any, Mapping


class FailureKind(str, Enum):
    NONE = "none"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    NETWORK = "network"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_LIMIT = "context_limit"
    EXECUTABLE_MISSING = "executable_missing"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    PROCESS_ERROR = "process_error"
    OUTPUT_ERROR = "output_error"
    EMPTY_OUTPUT = "empty_output"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutorCapabilities:
    structured_output: bool = False
    workspace_access: bool = False
    reasoning_effort: bool = False
    usage_reporting: bool = False
    local_execution: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "structured_output": self.structured_output,
            "workspace_access": self.workspace_access,
            "reasoning_effort": self.reasoning_effort,
            "usage_reporting": self.usage_reporting,
            "local_execution": self.local_execution,
        }


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_tokens is None:
            known = [self.input_tokens, self.output_tokens]
            if all(value is not None for value in known):
                object.__setattr__(self, "total_tokens", sum(value or 0 for value in known))

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    model: str
    cwd: Path
    timeout_seconds: float = 300.0
    system_prompt: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "read-only"
    trace_path: Path | None = None
    output_path: Path | None = None
    output_schema: Mapping[str, Any] | Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def full_prompt(self) -> str:
        if not self.system_prompt:
            return self.prompt
        return f"{self.system_prompt.rstrip()}\n\n{self.prompt.lstrip()}"


@dataclass(frozen=True)
class GenerationResult:
    executor: str
    model: str
    success: bool
    output: str = ""
    stderr: str = ""
    returncode: int | None = None
    duration_seconds: float = 0.0
    usage: Usage = field(default_factory=Usage)
    failure_kind: FailureKind = FailureKind.NONE
    failure_message: str | None = None
    capabilities: ExecutorCapabilities = field(default_factory=ExecutorCapabilities)
    trace_path: Path | None = None
    output_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "model": self.model,
            "success": self.success,
            "output": self.output,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "usage": self.usage.to_dict(),
            "failure_kind": self.failure_kind.value,
            "failure_message": self.failure_message,
            "capabilities": self.capabilities.to_dict(),
            "trace_path": str(self.trace_path) if self.trace_path else None,
            "output_path": str(self.output_path) if self.output_path else None,
            "metadata": dict(self.metadata),
        }


class GenerationExecutor(ABC):
    name: str
    capabilities: ExecutorCapabilities

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate one response without mutating the request."""


_FAILURE_PATTERNS: tuple[tuple[FailureKind, re.Pattern[str]], ...] = (
    (FailureKind.RATE_LIMIT, re.compile(r"(?:\b429\b|too many requests|rate.?limit|quota exceeded)", re.I)),
    (FailureKind.AUTHENTICATION, re.compile(r"(?:\b401\b|\b403\b|unauthori[sz]ed|authentication|invalid api.?key|login required)", re.I)),
    (FailureKind.NETWORK, re.compile(r"(?:connection (?:reset|refused|closed)|timed? out connecting|dns|name resolution|network|tls|ssl|stream disconnected|proxy)", re.I)),
    (FailureKind.MODEL_NOT_FOUND, re.compile(r"(?:unknown model|model.+not found|model.+does not exist|pull model)", re.I)),
    (FailureKind.CONTEXT_LIMIT, re.compile(r"(?:context (?:length|window)|maximum context|too many tokens)", re.I)),
    (FailureKind.EXECUTABLE_MISSING, re.compile(r"(?:not recognized as an internal|command not found|no such file or directory|executable.+not found)", re.I)),
    (FailureKind.INVALID_REQUEST, re.compile(r"(?:\b400\b|invalid (?:request|argument))", re.I)),
    (FailureKind.CANCELLED, re.compile(r"(?:cancelled|canceled|keyboard interrupt|ctrl-c)", re.I)),
    (FailureKind.EMPTY_OUTPUT, re.compile(r"(?:missing final output|empty model output)", re.I)),
    (FailureKind.OUTPUT_ERROR, re.compile(r"malformed output", re.I)),
)


def classify_failure(
    text: str = "",
    *,
    returncode: int | None = None,
    timed_out: bool = False,
    executable_missing: bool = False,
) -> FailureKind:
    if timed_out:
        return FailureKind.TIMEOUT
    if executable_missing:
        return FailureKind.EXECUTABLE_MISSING
    for kind, pattern in _FAILURE_PATTERNS:
        if pattern.search(text or ""):
            return kind
    if returncode not in (None, 0):
        return FailureKind.PROCESS_ERROR
    if text:
        return FailureKind.UNKNOWN
    return FailureKind.NONE
