from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evals.executors import (
    ExecutorCapabilities,
    FailureKind,
    GenerationExecutor,
    GenerationRequest,
    GenerationResult,
    OllamaCliExecutor,
    attempt_summary,
    generate_with_transport_retries,
)
from evals.executors import codex_cli


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class OllamaExecutorTests(unittest.TestCase):
    def test_http_generation_records_output_usage_and_schema(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: float) -> _Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response(
                {
                    "model": "qwen3.5:9b",
                    "response": '{"ok":true}',
                    "done": True,
                    "prompt_eval_count": 11,
                    "eval_count": 5,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_path = root / "output.json"
            trace_path = root / "trace.json"
            request = GenerationRequest(
                prompt="Return JSON",
                model="qwen3.5:9b",
                cwd=root,
                timeout_seconds=12,
                output_schema={"type": "object"},
                output_path=output_path,
                trace_path=trace_path,
            )
            with patch("evals.executors.ollama_cli.urllib_request.urlopen", fake_urlopen):
                result = OllamaCliExecutor().generate(request)

            self.assertTrue(result.success)
            self.assertEqual(result.output, '{"ok":true}')
            self.assertEqual(result.usage.input_tokens, 11)
            self.assertEqual(result.usage.output_tokens, 5)
            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"ok":true}')
            self.assertEqual(captured["timeout"], 12)
            sent = json.loads(captured["request"].data.decode("utf-8"))
            self.assertFalse(sent["stream"])
            self.assertFalse(sent["think"])
            self.assertEqual(sent["format"], {"type": "object"})

    def test_timeout_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = GenerationRequest(
                prompt="test",
                model="qwen3.5:9b",
                cwd=Path(directory),
                timeout_seconds=1,
            )
            with patch(
                "evals.executors.ollama_cli.urllib_request.urlopen",
                side_effect=socket.timeout("timed out"),
            ):
                result = OllamaCliExecutor().generate(request)

        self.assertFalse(result.success)
        self.assertEqual(result.failure_kind, FailureKind.TIMEOUT)

    def test_host_rejects_credentials_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = GenerationRequest(
                prompt="test",
                model="qwen3.5:9b",
                cwd=Path(directory),
                environment={"OLLAMA_HOST": "http://user:secret@localhost:11434/private"},
            )
            result = OllamaCliExecutor().generate(request)

        self.assertFalse(result.success)
        self.assertEqual(result.failure_kind, FailureKind.INVALID_REQUEST)


class CodexExecutorTests(unittest.TestCase):
    def test_failure_message_prefers_provider_error_over_lifecycle_events(self) -> None:
        detail = "\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "error", "message": "403 Forbidden: insufficient balance"}),
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "403 Forbidden: insufficient balance"},
            }),
        ))

        message = codex_cli.CodexCliExecutor._failure_message(
            FailureKind.AUTHENTICATION, detail
        )

        self.assertIn("insufficient balance", message)
        self.assertNotIn("thread.started", message)

    def test_command_binds_workspace_write_sandbox_to_request_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            request = GenerationRequest(
                prompt="Create config.json",
                model="model",
                cwd=cwd,
                sandbox="workspace-write",
            )
            command = codex_cli._build_command(
                ["codex"], request, cwd / "last-message.txt", None
            )

        sandbox_index = command.index("--sandbox")
        cwd_index = command.index("--cd")
        self.assertEqual(command[sandbox_index + 1], "workspace-write")
        self.assertEqual(command[cwd_index + 1], str(cwd.resolve()))

    def test_read_only_router_error_invalidates_workspace_write_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = GenerationRequest(
                prompt="Create config.json",
                model="model",
                cwd=Path(directory),
                sandbox="workspace-write",
            )

        self.assertTrue(
            codex_cli._workspace_write_denied(
                request,
                "patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings",
            )
        )


class _SequenceExecutor(GenerationExecutor):
    name = "sequence"
    capabilities = ExecutorCapabilities()

    def __init__(self, results: list[GenerationResult]) -> None:
        self.results = list(results)
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return self.results.pop(0)


class TransportRecoveryTests(unittest.TestCase):
    def test_timeout_retry_uses_backoff_and_records_zero_token_timeout(self) -> None:
        timeout = GenerationResult(
            executor="sequence",
            model="model",
            success=False,
            failure_kind=FailureKind.TIMEOUT,
        )
        success = GenerationResult(executor="sequence", model="model", success=True, output="ok")
        executor = _SequenceExecutor([timeout, success])
        delays: list[float] = []

        with tempfile.TemporaryDirectory() as directory:
            request = GenerationRequest(prompt="test", model="model", cwd=Path(directory))
            execution = generate_with_transport_retries(
                executor,
                request,
                2,
                backoff_seconds=3,
                sleeper=delays.append,
            )

        self.assertTrue(execution.result.success)
        self.assertEqual(execution.retry_count, 1)
        self.assertEqual(delays, [3])
        self.assertEqual(executor.requests[1].metadata["transport_retry_delay_seconds"], 3)
        summary = attempt_summary(timeout)
        self.assertTrue(summary["recoverable"])
        self.assertTrue(summary["zero_token_timeout"])

    def test_non_retryable_failure_stops_without_backoff(self) -> None:
        authentication = GenerationResult(
            executor="sequence",
            model="model",
            success=False,
            failure_kind=FailureKind.AUTHENTICATION,
        )
        executor = _SequenceExecutor([authentication])
        delays: list[float] = []

        with tempfile.TemporaryDirectory() as directory:
            request = GenerationRequest(prompt="test", model="model", cwd=Path(directory))
            execution = generate_with_transport_retries(
                executor,
                request,
                3,
                backoff_seconds=3,
                sleeper=delays.append,
            )

        self.assertEqual(len(execution.attempts), 1)
        self.assertEqual(delays, [])

    def test_codex_call_slot_serializes_concurrent_calls(self) -> None:
        active = 0
        maximum_active = 0
        state_lock = threading.Lock()

        def enter_slot() -> None:
            nonlocal active, maximum_active
            with codex_cli._codex_call_slot():
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.02)
                with state_lock:
                    active -= 1

        with (
            patch.object(codex_cli, "_CODEX_SEMAPHORE", threading.BoundedSemaphore(1)),
            patch.object(codex_cli, "_CODEX_COOLDOWN_SECONDS", 0),
            patch.object(codex_cli, "_CODEX_LAST_FINISHED_AT", 0),
        ):
            threads = [threading.Thread(target=enter_slot) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

        self.assertEqual(maximum_active, 1)


if __name__ == "__main__":
    unittest.main()
