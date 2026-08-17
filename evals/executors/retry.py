"""Transport retries that remain separate from plan and artifact repair."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import shutil
import time
from typing import Callable

from .base import FailureKind, GenerationExecutor, GenerationRequest, GenerationResult


RETRYABLE_FAILURES = {
    FailureKind.TIMEOUT,
    FailureKind.RATE_LIMIT,
    FailureKind.NETWORK,
    FailureKind.PROCESS_ERROR,
    FailureKind.OUTPUT_ERROR,
    FailureKind.EMPTY_OUTPUT,
}


def transport_retry_policy() -> dict[str, float]:
    raw_backoff = os.environ.get("CONSTRAINT_EXEC_RETRY_BACKOFF_SECONDS", "15")
    try:
        backoff_seconds = float(raw_backoff)
    except ValueError as error:
        raise ValueError("CONSTRAINT_EXEC_RETRY_BACKOFF_SECONDS must be numeric") from error
    if backoff_seconds < 0:
        raise ValueError("CONSTRAINT_EXEC_RETRY_BACKOFF_SECONDS cannot be negative")
    return {"backoff_seconds": backoff_seconds, "max_backoff_seconds": 60.0}


@dataclass(frozen=True)
class TransportExecution:
    result: GenerationResult
    attempts: tuple[GenerationResult, ...]

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)


def attempt_summary(result: GenerationResult) -> dict[str, object]:
    zero_token_timeout = (
        result.failure_kind == FailureKind.TIMEOUT
        and result.usage.total_tokens in (None, 0)
    )
    return {
        "executor": result.executor,
        "model": result.model,
        "success": result.success,
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
        "usage": result.usage.to_dict(),
        "failure_kind": result.failure_kind.value,
        "failure_message": result.failure_message,
        "recoverable": result.failure_kind in RETRYABLE_FAILURES,
        "zero_token_timeout": zero_token_timeout,
        "trace_path": str(result.trace_path) if result.trace_path else None,
        "output_path": str(result.output_path) if result.output_path else None,
        "metadata": dict(result.metadata),
    }


def _attempt_path(path: Path | None, index: int, total: int) -> Path | None:
    if path is None or total == 1:
        return path
    return path.with_name(f"{path.stem}.transport-{index:02d}{path.suffix}")


def _publish_attempt(source: Path | None, destination: Path | None) -> None:
    if source is None or destination is None or source == destination or not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def generate_with_transport_retries(
    executor: GenerationExecutor,
    request: GenerationRequest,
    max_attempts: int = 1,
    *,
    backoff_seconds: float | None = None,
    max_backoff_seconds: float = 60.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> TransportExecution:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if backoff_seconds is None:
        backoff_seconds = transport_retry_policy()["backoff_seconds"]
    if backoff_seconds < 0 or max_backoff_seconds < 0:
        raise ValueError("retry backoff values cannot be negative")
    attempts: list[GenerationResult] = []
    delay_before_attempt = 0.0
    for index in range(1, max_attempts + 1):
        if delay_before_attempt > 0:
            sleeper(delay_before_attempt)
        attempt_request = replace(
            request,
            trace_path=_attempt_path(request.trace_path, index, max_attempts),
            output_path=_attempt_path(request.output_path, index, max_attempts),
            metadata={
                **request.metadata,
                "transport_attempt": index,
                "transport_attempts": max_attempts,
                "transport_retry_delay_seconds": delay_before_attempt,
            },
        )
        result = executor.generate(attempt_request)
        attempts.append(result)
        if result.success or result.failure_kind not in RETRYABLE_FAILURES:
            break
        delay_before_attempt = min(
            max_backoff_seconds,
            backoff_seconds * (2 ** (index - 1)),
        )
    final = attempts[-1]
    _publish_attempt(final.output_path, request.output_path)
    _publish_attempt(final.trace_path, request.trace_path)
    if final.output_path != request.output_path or final.trace_path != request.trace_path:
        final = replace(final, output_path=request.output_path, trace_path=request.trace_path)
    return TransportExecution(final, tuple(attempts))
