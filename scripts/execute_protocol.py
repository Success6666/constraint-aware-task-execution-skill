"""Execute the constraint-aware protocol from a stable JSON request."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

from execution_state import ExecutionState, RetryBudget, Stage, write_checkpoint  # noqa: E402
from executors import (  # noqa: E402
    CodexCliExecutor,
    GenerationRequest,
    OllamaCliExecutor,
    attempt_summary,
    generate_with_transport_retries,
)
from protocol import parse_plan  # noqa: E402
from redaction import redact_text  # noqa: E402
from runtime_env import ALLOWED_ENVIRONMENT_OVERRIDES  # noqa: E402
from validators import validate_workspace_contract  # noqa: E402


PROTOCOL_VERSION = "1.0"
PROTOCOL_ID = "constraint-exec/v1"
CHECKPOINT_VERSION = "1.0"
PLAN_SCHEMA = EVALS / "schemas" / "execution-plan.schema.json"
SUPPORTED_EXECUTORS = {"codex", "codex-cli", "ollama", "ollama-cli"}
SUPPORTED_SANDBOXES = {"read-only", "workspace-write"}
REQUEST_FIELDS = {
    "allowed_paths",
    "budgets",
    "environment",
    "executor",
    "output_root",
    "prompt",
    "result_path",
    "resume",
    "run_id",
    "sandbox",
    "schema_version",
    "structured_plan",
    "timeout_seconds",
    "validators",
    "workspace",
    "workspace_required",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a versioned constraint-aware execution request.")
    parser.add_argument("request", type=Path, help="JSON request matching runtime-request.schema.json")
    parser.add_argument("--output", type=Path, help="Result path; defaults to request output.result_path")
    parser.add_argument("--workspace-root", type=Path, help="Trusted root containing the requested workspace")
    parser.add_argument("--artifact-root", type=Path, help="Trusted root containing checkpoints, traces, and results")
    return parser.parse_args()


def load_request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    unknown = sorted(set(value) - REQUEST_FIELDS)
    if unknown:
        raise ValueError(f"unsupported request fields: {', '.join(unknown)}")
    if value.get("schema_version") != PROTOCOL_VERSION:
        raise ValueError(f"unsupported schema_version: {value.get('schema_version')}")
    for key in ("run_id", "prompt", "workspace"):
        if not isinstance(value.get(key), str) or not str(value[key]).strip():
            raise ValueError(f"{key} must be a non-empty string")
    executor = value.get("executor")
    if not isinstance(executor, dict):
        raise ValueError("executor must be an object")
    executor_type = str(executor.get("type", "")).strip().casefold()
    if executor_type not in SUPPORTED_EXECUTORS:
        raise ValueError(f"unsupported executor: {executor_type or '<empty>'}")
    model = executor.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("executor.model must be a non-empty string")
    effort = executor.get("reasoning_effort")
    if effort is not None and effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("executor.reasoning_effort is invalid")
    for key in ("structured_plan", "workspace_required", "resume"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"{key} must be boolean")
    sandbox = value.get("sandbox", "workspace-write")
    if sandbox not in SUPPORTED_SANDBOXES:
        raise ValueError(f"sandbox must be one of: {', '.join(sorted(SUPPORTED_SANDBOXES))}")
    timeout = value.get("timeout_seconds", 300)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
        raise ValueError("timeout_seconds must be between 0 and 3600")
    environment = value.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in environment.items()
    ):
        raise ValueError("environment must contain string keys and values")
    rejected_environment = sorted(set(environment) - ALLOWED_ENVIRONMENT_OVERRIDES)
    if rejected_environment:
        raise ValueError(
            "environment contains unsupported overrides: " + ", ".join(rejected_environment)
        )
    allowed_paths = value.get("allowed_paths", [])
    if not isinstance(allowed_paths, list) or not all(
        isinstance(item, str) and item.strip() for item in allowed_paths
    ):
        raise ValueError("allowed_paths must contain non-empty strings")
    for item in allowed_paths:
        candidate = Path(item)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise ValueError(f"allowed path must stay relative to workspace: {item}")
    validators = value.get("validators", [])
    if not isinstance(validators, list) or not all(isinstance(item, dict) for item in validators):
        raise ValueError("validators must be an array of objects")
    for index, validator in enumerate(validators):
        if not isinstance(validator.get("type"), str) or not validator["type"].strip():
            raise ValueError(f"validators[{index}].type must be a non-empty string")
        if "required" in validator and not isinstance(validator["required"], bool):
            raise ValueError(f"validators[{index}].required must be boolean")
    budgets = value.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("budgets must be an object")
    unknown_budgets = sorted(set(budgets) - {"transport", "plan", "artifact"})
    if unknown_budgets:
        raise ValueError(f"unsupported budget fields: {', '.join(unknown_budgets)}")
    for key, minimum in (("transport", 1), ("plan", 1), ("artifact", 0)):
        if key in budgets and (
            not isinstance(budgets[key], int) or isinstance(budgets[key], bool) or budgets[key] < minimum
        ):
            raise ValueError(f"budgets.{key} must be an integer >= {minimum}")
    for key in ("output_root", "result_path"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"{key} must be a non-empty path string")
    return value


def select_executor(config: Mapping[str, Any]):
    kind = str(config.get("type", "codex-cli")).strip().casefold()
    if kind in {"codex", "codex-cli"}:
        return CodexCliExecutor()
    if kind in {"ollama", "ollama-cli"}:
        return OllamaCliExecutor()
    raise ValueError(f"unsupported executor: {kind}")


def plan_prompt(user_prompt: str, previous: str | None = None, errors: list[dict[str, str]] | None = None) -> str:
    retry = ""
    if previous is not None:
        retry = (
            "\nRegenerate the plan. Use only these machine issues and preserve valid fields."
            f"\nISSUES={json.dumps(errors or [], ensure_ascii=False)}"
            f"\nPREVIOUS_PLAN={previous}"
        )
    return (
        "Return only one JSON execution plan matching the supplied schema. Preserve the full objective and list all "
        "independent non-constraint requirements with observable acceptance criteria. Separate constraints from "
        "implementation strategies. Set required_gate=true "
        "only for explicit enforcement or a necessary safety boundary; otherwise use false and an empty "
        "failure_action. Do not add validators that are not justified by an observable contract."
        f"\nUSER_REQUEST={user_prompt}{retry}"
    )


def execution_prompt(user_prompt: str, plan: str | None, allowed_paths: list[str]) -> str:
    return (
        "Complete the full user objective in the current workspace. Preserve non-constraint requirements and "
        "modify only allowed paths. Validation is external; do not add compliance-only gates or discuss scores."
        f"\nALLOWED_PATHS={json.dumps(allowed_paths, ensure_ascii=False)}"
        f"\nUSER_REQUEST={user_prompt}"
        + (f"\nVALIDATED_PLAN={plan}" if plan else "")
    )


def repair_prompt(user_prompt: str, plan: str | None, allowed_paths: list[str], errors: list[str]) -> str:
    return (
        "Repair only the affected artifact portions in the current workspace, then leave the complete result in "
        "place. Revalidate previously satisfied requirements mentally, but use only the machine errors below as "
        "repair feedback. Do not discuss evaluation metrics."
        f"\nALLOWED_PATHS={json.dumps(allowed_paths, ensure_ascii=False)}"
        f"\nERRORS={json.dumps(errors, ensure_ascii=False)}"
        f"\nUSER_REQUEST={user_prompt}"
        + (f"\nVALIDATED_PLAN={plan}" if plan else "")
    )


def invoke(
    executor: Any,
    request: Mapping[str, Any],
    prompt: str,
    workspace: Path,
    output_root: Path,
    phase: str,
    index: int,
    *,
    output_schema: Path | None = None,
    transport_attempts: int = 1,
) -> tuple[Any, list[dict[str, Any]]]:
    executor_config = request["executor"]
    trace = output_root / "attempts" / f"{phase}-{index:02d}.trace"
    output = output_root / "attempts" / f"{phase}-{index:02d}.out"
    schema = output_schema if output_schema is not None and executor.capabilities.structured_output else None
    generation = GenerationRequest(
        prompt=prompt,
        model=str(executor_config.get("model", "")),
        cwd=workspace,
        timeout_seconds=float(request.get("timeout_seconds", 300)),
        reasoning_effort=executor_config.get("reasoning_effort"),
        sandbox=str(request.get("sandbox", "workspace-write")),
        trace_path=trace,
        output_path=output,
        output_schema=schema,
        environment=request.get("environment", {}),
        metadata={"run_id": request["run_id"], "phase": phase, "attempt": index},
    )
    execution = generate_with_transport_retries(executor, generation, transport_attempts)
    return execution.result, [attempt_summary(item) for item in execution.attempts]


def error_codes(validations: list[dict[str, Any]]) -> list[str]:
    return [str(error) for result in validations for error in result.get("errors", [])]


def workspace_fingerprints(workspace: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(workspace).as_posix()
            target = str(path.readlink())
            fingerprints[relative] = hashlib.sha256(f"symlink:{target}".encode("utf-8")).hexdigest()
            continue
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".codex" in path.parts
            or "__pycache__" in path.parts
            or path.name == "AGENTS.md"
        ):
            continue
        relative = path.relative_to(workspace).as_posix()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        fingerprints[relative] = digest.hexdigest()
    return fingerprints


def changed_workspace_paths(before: Mapping[str, str], workspace: Path) -> list[str]:
    after = workspace_fingerprints(workspace)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _request_digest(request: Mapping[str, Any]) -> str:
    stable = {key: value for key, value in request.items() if key != "resume"}
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _contained_path(raw: str | Path, root: Path, label: str) -> Path:
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    trusted = root.resolve()
    if resolved != trusted and trusted not in resolved.parents:
        raise ValueError(f"{label} escapes trusted root: {resolved}")
    return resolved


def _checkpoint_payload(
    request: Mapping[str, Any],
    workspace: Path,
    output_root: Path,
    state: ExecutionState,
    *,
    plan_text: str | None,
    plan_validation: Mapping[str, Any] | None,
    attempts: list[dict[str, Any]],
    before_execution: Mapping[str, str] | None,
    validations: list[dict[str, Any]],
    inflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "protocol": PROTOCOL_ID,
        "request_digest": _request_digest(request),
        "workspace": str(workspace),
        "output_root": str(output_root),
        "state": state.to_dict(),
        "plan_text": plan_text,
        "plan_validation": dict(plan_validation) if plan_validation is not None else None,
        "attempts": attempts,
        "before_execution": dict(before_execution) if before_execution is not None else None,
        "validations": validations,
        "inflight": dict(inflight) if inflight is not None else None,
    }


def _result(
    request: Mapping[str, Any],
    executor: Any,
    state: ExecutionState,
    plan_validation: Mapping[str, Any] | None,
    validations: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": PROTOCOL_VERSION,
        "protocol": PROTOCOL_ID,
        "run_id": request["run_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "success": state.completed,
        "termination_reason": state.termination_reason,
        "executor": {
            "type": executor.name,
            "model": request["executor"].get("model", ""),
            "capabilities": executor.capabilities.to_dict(),
        },
        "plan_validation": plan_validation,
        "validations": validations,
        "state": state.to_dict(),
        "attempts": attempts,
    }


def execute(
    request: Mapping[str, Any],
    result_path: Path,
    *,
    workspace_root: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    requested_workspace = Path(str(request["workspace"]))
    trusted_workspace_root = (workspace_root or requested_workspace.resolve()).resolve()
    workspace = _contained_path(requested_workspace, trusted_workspace_root, "workspace")
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    trusted_artifact_root = (artifact_root or result_path.resolve().parent).resolve()
    result_path = _contained_path(result_path, trusted_artifact_root, "result_path")
    output_root = _contained_path(
        str(request.get("output_root", str(request["run_id"]))),
        trusted_artifact_root,
        "output_root",
    )
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / "checkpoint.json"
    resume = bool(request.get("resume", False))
    attempts: list[dict[str, Any]] = []
    plan_text: str | None = None
    plan_validation: dict[str, Any] | None = None
    before_execution: dict[str, str] | None = None
    validations: list[dict[str, Any]] = []
    inflight: dict[str, Any] | None = None
    if resume and state_path.is_file():
        checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
        if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise ValueError("checkpoint version is not supported")
        if checkpoint.get("protocol") != PROTOCOL_ID:
            raise ValueError("checkpoint protocol does not match this runtime")
        if checkpoint.get("request_digest") != _request_digest(request):
            raise ValueError("checkpoint request digest does not match the request")
        if checkpoint.get("workspace") != str(workspace) or checkpoint.get("output_root") != str(output_root):
            raise ValueError("checkpoint paths do not match the request")
        state = ExecutionState.from_dict(checkpoint["state"])
        plan_text = checkpoint.get("plan_text")
        plan_validation = checkpoint.get("plan_validation")
        attempts = list(checkpoint.get("attempts", []))
        baseline = checkpoint.get("before_execution")
        before_execution = dict(baseline) if isinstance(baseline, dict) else None
        validations = list(checkpoint.get("validations", []))
        inflight = checkpoint.get("inflight")
    else:
        state = ExecutionState(str(request["run_id"]))
    budgets_config = request.get("budgets", {})
    budget = RetryBudget(
        transport=int(budgets_config.get("transport", 1)),
        plan=int(budgets_config.get("plan", 2)),
        artifact=int(budgets_config.get("artifact", 2)),
    )
    executor = select_executor(request["executor"])
    structured_plan = bool(request.get("structured_plan", True))
    workspace_required = bool(request.get("workspace_required", True))
    allowed_paths = [str(path) for path in request.get("allowed_paths", [])]
    validators = list(request.get("validators", []))

    def checkpoint(active: Mapping[str, Any] | None = None) -> None:
        write_checkpoint(
            state_path,
            _checkpoint_payload(
                request,
                workspace,
                output_root,
                state,
                plan_text=plan_text,
                plan_validation=plan_validation,
                attempts=attempts,
                before_execution=before_execution,
                validations=validations,
                inflight=active,
            ),
        )

    if inflight is not None:
        state.finish(False, "resume_inflight_unknown")
        checkpoint()
    if state.stage in {Stage.COMPLETE.value, Stage.FAILED.value}:
        result = _result(request, executor, state, plan_validation, validations, attempts)
        write_checkpoint(result_path, result)
        return result

    if workspace_required and not executor.capabilities.workspace_access:
        state.finish(False, "executor_capability_mismatch")
        checkpoint()
    elif structured_plan and plan_text is None:
        previous: str | None = None
        issues: list[dict[str, str]] = []
        while state.can_attempt(Stage.PLAN, budget):
            index = state.count(Stage.PLAN) + 1
            checkpoint({"phase": "plan", "index": index})
            generated, transport_records = invoke(
                executor,
                request,
                plan_prompt(str(request["prompt"]), previous, issues),
                workspace,
                output_root,
                "plan",
                index,
                output_schema=PLAN_SCHEMA,
                transport_attempts=budget.transport,
            )
            attempt = generated.to_dict()
            attempts.extend(transport_records)
            if generated.success:
                previous = generated.output
                _plan, validation = parse_plan(previous, allow_legacy_aliases=False)
                plan_validation = validation.to_dict()
                codes = [issue.code for issue in validation.issues]
                state.add_attempt(Stage.PLAN, "pass" if validation.valid else "fail", errors=codes, evidence={"generation": attempt, "validation": plan_validation})
                if validation.valid:
                    plan_text = previous
                    break
                issues = [{"code": issue.code, "path": issue.path} for issue in validation.issues]
            else:
                code = f"EXECUTOR_{generated.failure_kind.value.upper()}"
                state.add_attempt(Stage.PLAN, "fail", errors=[code], evidence={"generation": attempt})
                issues = [{"code": code, "path": "$"}]
            checkpoint()
        if plan_text is None:
            state.finish(False, "plan_validation_exhausted")
            checkpoint()

    executed = any(item.stage == Stage.EXECUTE.value and item.status == "pass" for item in state.attempts)
    if state.stage != Stage.FAILED.value and not executed:
        before_execution = workspace_fingerprints(workspace)
        checkpoint({"phase": "execute", "index": 1})
        generated, transport_records = invoke(
            executor,
            request,
            execution_prompt(str(request["prompt"]), plan_text, allowed_paths),
            workspace,
            output_root,
            "execute",
            1,
            transport_attempts=budget.transport,
        )
        attempt = generated.to_dict()
        attempts.extend(transport_records)
        changed_paths = changed_workspace_paths(before_execution, workspace)
        state.add_attempt(
            Stage.EXECUTE,
            "pass" if generated.success else "fail",
            errors=[] if generated.success else [f"EXECUTOR_{generated.failure_kind.value.upper()}"],
            changed_paths=changed_paths,
            evidence={"generation": attempt},
        )
        if not generated.success:
            state.finish(False, "execution_failed")
        checkpoint()

    if state.stage != Stage.FAILED.value:
        if before_execution is None:
            raise ValueError("checkpoint is missing the execution baseline")
        changed_paths = changed_workspace_paths(before_execution, workspace)
        validations = validate_workspace_contract(
            workspace,
            allowed_paths,
            validators,
            changed_paths=changed_paths,
        )
        state.update_validations(validations)
        failures = error_codes([item for item in validations if item["status"] == "fail"])
        required_unsupported = [
            item
            for item in validations
            if item["status"] == "unsupported" and item.get("required", True)
        ]
        state.add_attempt(Stage.VALIDATE, "pass" if not failures and not required_unsupported else "fail", errors=failures + [f"VALIDATOR_UNSUPPORTED:{item['type']}" for item in required_unsupported], evidence={"validations": validations})
        checkpoint()
        while failures and state.can_attempt(Stage.REPAIR, budget):
            index = state.count(Stage.REPAIR) + 1
            before_repair = workspace_fingerprints(workspace)
            checkpoint({"phase": "repair", "index": index})
            repaired, transport_records = invoke(
                executor,
                request,
                repair_prompt(str(request["prompt"]), plan_text, allowed_paths, failures),
                workspace,
                output_root,
                "repair",
                index,
                transport_attempts=budget.transport,
            )
            repair_record = repaired.to_dict()
            attempts.extend(transport_records)
            changed_paths = changed_workspace_paths(before_repair, workspace)
            state.add_attempt(
                Stage.REPAIR,
                "pass" if repaired.success else "fail",
                errors=[] if repaired.success else [f"EXECUTOR_{repaired.failure_kind.value.upper()}"],
                changed_paths=changed_paths,
                evidence={"generation": repair_record},
            )
            if not repaired.success:
                continue
            validations = validate_workspace_contract(
                workspace,
                allowed_paths,
                validators,
                changed_paths=changed_workspace_paths(before_execution, workspace),
            )
            state.update_validations(validations)
            failures = error_codes([item for item in validations if item["status"] == "fail"])
            required_unsupported = [
                item
                for item in validations
                if item["status"] == "unsupported" and item.get("required", True)
            ]
            validation_errors = failures + [
                f"VALIDATOR_UNSUPPORTED:{item['type']}" for item in required_unsupported
            ]
            state.add_attempt(
                Stage.VALIDATE,
                "pass" if not validation_errors else "fail",
                errors=validation_errors,
                evidence={"validations": validations},
            )
            checkpoint()
        if required_unsupported:
            state.finish(False, "validation_unsupported")
        elif failures:
            state.finish(False, "artifact_validation_exhausted")
        else:
            state.finish(True, "success")

    checkpoint()
    result = _result(request, executor, state, plan_validation, validations, attempts)
    write_checkpoint(result_path, result)
    return result


def main() -> int:
    args = parse_args()
    try:
        request = load_request(args.request)
        request_path = args.request.resolve()
        workspace_root = (args.workspace_root or request_path.parent).resolve()
        artifact_root = (args.artifact_root or request_path.parent).resolve()
        configured_result = args.output or Path(str(request.get("result_path", request_path.with_suffix(".result.json").name)))
        result_path = _contained_path(configured_result, artifact_root, "result_path")
        result = execute(
            request,
            result_path,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        result = {
            "schema_version": PROTOCOL_VERSION,
            "protocol": PROTOCOL_ID,
            "run_id": "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "success": False,
            "termination_reason": "invalid_request",
            "executor": {},
            "plan_validation": None,
            "validations": [],
            "state": {},
            "attempts": [],
            "error": {"kind": "invalid_request", "message": redact_text(str(error))},
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else (2 if result["termination_reason"] == "invalid_request" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
