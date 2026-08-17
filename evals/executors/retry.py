"""Transport retries that remain separate from plan and artifact repair."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import shutil

from .base import FailureKind, GenerationExecutor, GenerationRequest, GenerationResult


RETRYABLE_FAILURES = {
    FailureKind.TIMEOUT,
    FailureKind.RATE_LIMIT,
    FailureKind.NETWORK,
    FailureKind.PROCESS_ERROR,
    FailureKind.OUTPUT_ERROR,
    FailureKind.EMPTY_OUTPUT,
}


@dataclass(frozen=True)
class TransportExecution:
    result: GenerationResult
    attempts: tuple[GenerationResult, ...]

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)


def attempt_summary(result: GenerationResult) -> dict[str, object]:
    return {
        "executor": result.executor,
        "model": result.model,
        "success": result.success,
        "returncode": result.returncode,
        "duration_seconds": result.duration_seconds,
        "usage": result.usage.to_dict(),
        "failure_kind": result.failure_kind.value,
        "failure_message": result.failure_message,
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
) -> TransportExecution:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    attempts: list[GenerationResult] = []
    for index in range(1, max_attempts + 1):
        attempt_request = replace(
            request,
            trace_path=_attempt_path(request.trace_path, index, max_attempts),
            output_path=_attempt_path(request.output_path, index, max_attempts),
            metadata={**request.metadata, "transport_attempt": index, "transport_attempts": max_attempts},
        )
        result = executor.generate(attempt_request)
        attempts.append(result)
        if result.success or result.failure_kind not in RETRYABLE_FAILURES:
            break
    final = attempts[-1]
    _publish_attempt(final.output_path, request.output_path)
    _publish_attempt(final.trace_path, request.trace_path)
    if final.output_path != request.output_path or final.trace_path != request.trace_path:
        final = replace(final, output_path=request.output_path, trace_path=request.trace_path)
    return TransportExecution(final, tuple(attempts))
