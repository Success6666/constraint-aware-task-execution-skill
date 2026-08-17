"""Execute the constraint-aware protocol from a stable JSON request."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from validators import validate_workspace_contract  # noqa: E402


PROTOCOL_VERSION = "1.0"
PLAN_SCHEMA = EVALS / "schemas" / "execution-plan.schema.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a versioned constraint-aware execution request.")
    parser.add_argument("request", type=Path, help="JSON request matching runtime-request.schema.json")
    parser.add_argument("--output", type=Path, help="Result path; defaults to request output.result_path")
    return parser.parse_args()


def load_request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    if value.get("schema_version") != PROTOCOL_VERSION:
        raise ValueError(f"unsupported schema_version: {value.get('schema_version')}")
    for key in ("run_id", "prompt", "workspace", "executor"):
        if not value.get(key):
            raise ValueError(f"missing required field: {key}")
    return value


def select_executor(config: Mapping[str, Any]):
    kind = str(config.get("type", "codex-cli"))
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
        "Return only one JSON execution plan matching the supplied schema. Preserve the full objective and all "
        "non-constraint requirements. Separate constraints from implementation strategies. Set required_gate=true "
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


def execute(request: Mapping[str, Any], result_path: Path) -> dict[str, Any]:
    workspace = Path(str(request["workspace"])).resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    output_root = Path(str(request.get("output_root", result_path.parent / str(request["run_id"])))).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / "checkpoint.json"
    resume = bool(request.get("resume", False))
    if resume and state_path.is_file():
        state = ExecutionState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
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
    attempts: list[dict[str, Any]] = []
    plan_text: str | None = None
    plan_validation: dict[str, Any] | None = None

    if workspace_required and not executor.capabilities.workspace_access:
        state.finish(False, "executor_capability_mismatch")
    elif structured_plan:
        previous: str | None = None
        issues: list[dict[str, str]] = []
        while state.can_attempt(Stage.PLAN, budget):
            index = state.count(Stage.PLAN) + 1
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
                _plan, validation = parse_plan(previous)
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
            write_checkpoint(state_path, state.to_dict())
        if plan_text is None:
            state.finish(False, "plan_validation_exhausted")

    if state.stage != Stage.FAILED.value:
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
        state.add_attempt(Stage.EXECUTE, "pass" if generated.success else "fail", errors=[] if generated.success else [f"EXECUTOR_{generated.failure_kind.value.upper()}"], evidence={"generation": attempt})
        if not generated.success:
            state.finish(False, "execution_failed")

    validations: list[dict[str, Any]] = []
    if state.stage != Stage.FAILED.value:
        validations = validate_workspace_contract(workspace, allowed_paths, validators)
        state.update_validations(validations)
        failures = error_codes([item for item in validations if item["status"] == "fail"])
        required_unsupported = [item for item in validations if item["status"] == "unsupported" and next((spec.get("required", True) for spec in validators if spec.get("type") == item["type"]), True)]
        state.add_attempt(Stage.VALIDATE, "pass" if not failures and not required_unsupported else "fail", errors=failures + [f"VALIDATOR_UNSUPPORTED:{item['type']}" for item in required_unsupported], evidence={"validations": validations})
        while failures and state.can_attempt(Stage.REPAIR, budget):
            index = state.count(Stage.REPAIR) + 1
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
            state.add_attempt(Stage.REPAIR, "pass" if repaired.success else "fail", errors=[] if repaired.success else [f"EXECUTOR_{repaired.failure_kind.value.upper()}"], evidence={"generation": repair_record})
            if not repaired.success:
                continue
            validations = validate_workspace_contract(workspace, allowed_paths, validators)
            state.update_validations(validations)
            failures = error_codes([item for item in validations if item["status"] == "fail"])
            state.add_attempt(Stage.VALIDATE, "pass" if not failures else "fail", errors=failures, evidence={"validations": validations})
            write_checkpoint(state_path, state.to_dict())
        if required_unsupported:
            state.finish(False, "validation_unsupported")
        elif failures:
            state.finish(False, "artifact_validation_exhausted")
        else:
            state.finish(True, "success")

    write_checkpoint(state_path, state.to_dict())
    result = {
        "schema_version": PROTOCOL_VERSION,
        "protocol": "constraint-aware-execution",
        "run_id": request["run_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "success": state.completed,
        "termination_reason": state.termination_reason,
        "executor": {"type": executor.name, "model": request["executor"].get("model", ""), "capabilities": executor.capabilities.to_dict()},
        "plan_validation": plan_validation,
        "validations": validations,
        "state": state.to_dict(),
        "attempts": attempts,
    }
    write_checkpoint(result_path, result)
    return result


def main() -> int:
    args = parse_args()
    request = load_request(args.request)
    result_path = args.output or Path(str(request.get("result_path", args.request.with_suffix(".result.json"))))
    result = execute(request, result_path)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
