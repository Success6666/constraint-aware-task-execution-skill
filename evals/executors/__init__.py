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
from .codex_cli import CodexCliExecutor, codex_scheduler_policy
from .ollama_cli import OllamaCliExecutor
from .retry import (
    RETRYABLE_FAILURES,
    TransportExecution,
    attempt_summary,
    generate_with_transport_retries,
    transport_retry_policy,
)


def create_executor(name: str) -> GenerationExecutor:
    normalized = name.strip().casefold()
    if normalized in {"codex", "codex-cli"}:
        return CodexCliExecutor()
    if normalized in {"ollama", "ollama-cli"}:
        return OllamaCliExecutor()
    raise ValueError(f"Unknown executor: {name}")


def execution_runtime_policy(name: str) -> dict[str, object]:
    normalized = name.strip().casefold()
    policy: dict[str, object] = {"transport_retry": transport_retry_policy()}
    if normalized in {"codex", "codex-cli"}:
        policy["scheduler"] = codex_scheduler_policy()
    return policy

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
    "execution_runtime_policy",
    "attempt_summary",
    "RETRYABLE_FAILURES",
    "TransportExecution",
]
