"""Codex CLI execution backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

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
    from ..redaction import atomic_write_text, redact_text
    from ..runtime_env import clean_subprocess_environment, temporary_codex_home
except ImportError:  # Direct execution from the evals directory.
    from redaction import atomic_write_text, redact_text
    from runtime_env import clean_subprocess_environment, temporary_codex_home


def _codex_launcher() -> list[str]:
    executable = shutil.which("codex")
    if not executable:
        raise FileNotFoundError("codex executable not found")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        node = shutil.which("node")
        script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if node and script.is_file():
            return [node, str(script)]
    return [executable]


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def _usage_from_jsonl(stdout: str) -> Usage:
    latest: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = [event.get("usage"), event.get("token_usage")]
        payload = event.get("payload")
        if isinstance(payload, dict):
            candidates.extend((payload.get("usage"), payload.get("token_usage")))
        for candidate in candidates:
            if isinstance(candidate, dict):
                latest = candidate
    def integer(*names: str) -> int | None:
        for name in names:
            value = latest.get(name)
            if isinstance(value, int):
                return value
        return None
    return Usage(
        input_tokens=integer("input_tokens", "prompt_tokens"),
        output_tokens=integer("output_tokens", "completion_tokens"),
        cached_input_tokens=integer("cached_input_tokens", "cache_read_input_tokens"),
        reasoning_tokens=integer("reasoning_output_tokens", "reasoning_tokens"),
        total_tokens=integer("total_tokens"),
        raw=latest,
    )


class CodexCliExecutor(GenerationExecutor):
    name = "codex-cli"
    capabilities = ExecutorCapabilities(
        structured_output=True,
        workspace_access=True,
        reasoning_effort=True,
        usage_reporting=True,
        local_execution=False,
    )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.monotonic()
        if not request.cwd.is_dir():
            return self._failure(
                request,
                started,
                FailureKind.INVALID_REQUEST,
                f"workspace directory does not exist: {request.cwd}",
            )
        try:
            launcher = _codex_launcher()
        except FileNotFoundError as error:
            return self._failure(request, started, FailureKind.EXECUTABLE_MISSING, str(error))

        with temporary_codex_home() as codex_home:
            final_output = codex_home / "last-message.txt"
            schema_path: Path | None = None
            if isinstance(request.output_schema, Path):
                schema_path = request.output_schema.resolve()
            elif request.output_schema is not None:
                schema_path = codex_home / "output-schema.json"
                schema_path.write_text(
                    json.dumps(dict(request.output_schema), ensure_ascii=False),
                    encoding="utf-8",
                )
            command = launcher + [
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--config",
                "features.plugins=false",
                "--config",
                "features.apps=false",
            ]
            if request.reasoning_effort:
                command.extend(
                    ["--config", f'model_reasoning_effort="{request.reasoning_effort}"']
                )
            command.extend([
                "--model",
                request.model,
                "--sandbox",
                request.sandbox,
                "--skip-git-repo-check",
                "--output-last-message",
                str(final_output),
                "--json",
                "-",
            ])
            if schema_path is not None:
                command[-1:-1] = ["--output-schema", str(schema_path)]
            env = clean_subprocess_environment(request.environment)
            env["CODEX_HOME"] = str(codex_home)
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process: subprocess.Popen[str] | None = None
            timed_out = False
            try:
                process = subprocess.Popen(
                    command,
                    cwd=request.cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    creationflags=creationflags,
                )
                stdout, stderr = process.communicate(
                    input=request.full_prompt(), timeout=request.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                if process is not None:
                    _terminate_process_tree(process)
                    stdout, stderr = process.communicate()
                else:
                    stdout, stderr = "", ""
            except OSError as error:
                return self._failure(
                    request,
                    started,
                    classify_failure(
                        str(error), executable_missing=isinstance(error, FileNotFoundError)
                    ),
                    str(error),
                )

            output = final_output.read_text(encoding="utf-8") if final_output.is_file() else ""
            combined = f"{stdout}\n{stderr}".strip()
            returncode = process.returncode if process is not None else None
            success = not timed_out and returncode == 0 and bool(output.strip())
            failure_kind = FailureKind.NONE if success else classify_failure(
                combined or "empty model output",
                returncode=returncode,
                timed_out=timed_out,
            )
            trace = redact_text(combined)
            safe_output = redact_text(output)
            if request.trace_path:
                atomic_write_text(request.trace_path, trace + ("\n" if trace else ""))
            if request.output_path:
                atomic_write_text(request.output_path, safe_output)
            return GenerationResult(
                executor=self.name,
                model=request.model,
                success=success,
                output=safe_output,
                stderr=redact_text(stderr),
                returncode=returncode,
                duration_seconds=time.monotonic() - started,
                usage=_usage_from_jsonl(stdout),
                failure_kind=failure_kind,
                failure_message=None if success else self._failure_message(failure_kind, combined),
                capabilities=self.capabilities,
                trace_path=request.trace_path,
                output_path=request.output_path,
                metadata=request.metadata,
            )

    def _failure(
        self,
        request: GenerationRequest,
        started: float,
        kind: FailureKind,
        message: str,
    ) -> GenerationResult:
        safe_message = redact_text(message)
        if request.trace_path:
            atomic_write_text(request.trace_path, safe_message + "\n")
        if request.output_path:
            atomic_write_text(request.output_path, "")
        return GenerationResult(
            executor=self.name,
            model=request.model,
            success=False,
            duration_seconds=time.monotonic() - started,
            failure_kind=kind,
            failure_message=safe_message,
            capabilities=self.capabilities,
            trace_path=request.trace_path,
            output_path=request.output_path,
            metadata=request.metadata,
        )

    @staticmethod
    def _failure_message(kind: FailureKind, detail: str) -> str:
        first_line = next((line.strip() for line in detail.splitlines() if line.strip()), "")
        return redact_text(first_line or kind.value)
