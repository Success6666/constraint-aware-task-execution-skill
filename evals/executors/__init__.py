"""Model execution backends used by the evaluation runners."""

from .base import (
    ExecutorCapabilities,
    FailureKind,
    GenerationExecutor,
    GenerationRequest,
    GenerationResult,
    Usage,
    classify_failure,
)
from .codex_cli import CodexCliExecutor
from .ollama_cli import OllamaCliExecutor
from .retry import RETRYABLE_FAILURES, TransportExecution, attempt_summary, generate_with_transport_retries


def create_executor(name: str) -> GenerationExecutor:
    normalized = name.strip().casefold()
    if normalized in {"codex", "codex-cli"}:
        return CodexCliExecutor()
    if normalized in {"ollama", "ollama-cli"}:
        return OllamaCliExecutor()
    raise ValueError(f"Unknown executor: {name}")

__all__ = [
    "CodexCliExecutor",
    "ExecutorCapabilities",
    "FailureKind",
    "GenerationExecutor",
    "GenerationRequest",
    "GenerationResult",
    "OllamaCliExecutor",
    "Usage",
    "classify_failure",
    "create_executor",
    "generate_with_transport_retries",
    "attempt_summary",
    "RETRYABLE_FAILURES",
    "TransportExecution",
]
