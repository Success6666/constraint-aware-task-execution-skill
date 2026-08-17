"""Local Ollama CLI execution backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

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
    from ..runtime_env import clean_subprocess_environment
except ImportError:  # Direct execution from the evals directory.
    from redaction import atomic_write_text, redact_text
    from runtime_env import clean_subprocess_environment


class OllamaCliExecutor(GenerationExecutor):
    name = "ollama-cli"
    default_model = "qwen3.5:9b"
    capabilities = ExecutorCapabilities(
        structured_output=False,
        workspace_access=False,
        reasoning_effort=False,
        usage_reporting=False,
        local_execution=True,
    )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.monotonic()
        executable = shutil.which("ollama")
        model = request.model or self.default_model
        if not request.cwd.is_dir():
            message = redact_text(f"workspace directory does not exist: {request.cwd}")
            if request.trace_path:
                atomic_write_text(request.trace_path, message + "\n")
            if request.output_path:
                atomic_write_text(request.output_path, "")
            return GenerationResult(
                executor=self.name,
                model=model,
                success=False,
                duration_seconds=time.monotonic() - started,
                failure_kind=FailureKind.INVALID_REQUEST,
                failure_message=message,
                capabilities=self.capabilities,
                trace_path=request.trace_path,
                output_path=request.output_path,
                metadata=request.metadata,
            )
        if not executable:
            message = "ollama executable not found"
            if request.trace_path:
                atomic_write_text(request.trace_path, message + "\n")
            if request.output_path:
                atomic_write_text(request.output_path, "")
            return GenerationResult(
                executor=self.name,
                model=model,
                success=False,
                duration_seconds=time.monotonic() - started,
                failure_kind=FailureKind.EXECUTABLE_MISSING,
                failure_message=message,
                capabilities=self.capabilities,
                trace_path=request.trace_path,
                output_path=request.output_path,
                metadata=request.metadata,
            )

        command = [executable, "run", model]
        prompt = request.full_prompt()
        if request.output_schema is not None:
            try:
                if isinstance(request.output_schema, Path):
                    schema = request.output_schema.read_text(encoding="utf-8")
                else:
                    schema = json.dumps(dict(request.output_schema), ensure_ascii=False)
            except (OSError, TypeError, ValueError) as error:
                message = redact_text(f"invalid output schema: {error}")
                if request.trace_path:
                    atomic_write_text(request.trace_path, message + "\n")
                if request.output_path:
                    atomic_write_text(request.output_path, "")
                return GenerationResult(
                    executor=self.name,
                    model=model,
                    success=False,
                    duration_seconds=time.monotonic() - started,
                    failure_kind=FailureKind.INVALID_REQUEST,
                    failure_message=message,
                    capabilities=self.capabilities,
                    trace_path=request.trace_path,
                    output_path=request.output_path,
                    metadata=request.metadata,
                )
            prompt += (
                "\n\nReturn only a JSON value conforming to this schema. Do not use Markdown fences."
                f"\nJSON_SCHEMA:\n{schema}"
            )
        process: subprocess.Popen[str] | None = None
        timed_out = False
        try:
            process = subprocess.Popen(
                command,
                cwd=request.cwd,
                env=clean_subprocess_environment(request.environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            stdout, stderr = process.communicate(
                input=prompt, timeout=request.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            if process is not None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = "", ""
        except OSError as error:
            return GenerationResult(
                executor=self.name,
                model=model,
                success=False,
                duration_seconds=time.monotonic() - started,
                failure_kind=classify_failure(
                    str(error), executable_missing=isinstance(error, FileNotFoundError)
                ),
                failure_message=redact_text(str(error)),
                capabilities=self.capabilities,
                metadata=request.metadata,
            )

        safe_output = redact_text(stdout)
        safe_stderr = redact_text(stderr)
        trace = "\n".join(part for part in (safe_output, safe_stderr) if part)
        if request.trace_path:
            atomic_write_text(request.trace_path, trace + ("\n" if trace else ""))
        if request.output_path:
            atomic_write_text(request.output_path, safe_output)
        returncode = process.returncode if process is not None else None
        success = not timed_out and returncode == 0 and bool(safe_output.strip())
        kind = FailureKind.NONE if success else classify_failure(
            safe_stderr or ("empty model output" if not safe_output.strip() else ""),
            returncode=returncode,
            timed_out=timed_out,
        )
        return GenerationResult(
            executor=self.name,
            model=model,
            success=success,
            output=safe_output,
            stderr=safe_stderr,
            returncode=returncode,
            duration_seconds=time.monotonic() - started,
            usage=Usage(),
            failure_kind=kind,
            failure_message=None if success else kind.value,
            capabilities=self.capabilities,
            trace_path=request.trace_path,
            output_path=request.output_path,
            metadata=request.metadata,
        )
