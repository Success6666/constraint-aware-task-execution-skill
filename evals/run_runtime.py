"""Run real workspace-write direct versus full-V2 artifact experiments."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from execution_state import ExecutionState, Stage
from executors import create_executor
from experiment_variants import VARIANTS
from protocol import parse_plan
from validators import validate_workspace_contract
from run_matrix import (
    DEFAULT_OUTPUT_ROOT,
    SCHEMA_PATH,
    add_usage,
    plan_prompt,
    prepare_workspace,
    dispatch_generation,
    slug,
    usage_from_trace,
)


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "runtime_cases.json"
RUNTIME_PROTOCOL = "workspace-artifact-v2-v1"
MODES = ("direct", "full-v2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic workspace artifact experiments.")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--model", action="append", dest="models", required=True)
    parser.add_argument("--executor", choices=("codex", "ollama"), default="codex")
    parser.add_argument("--mode", action="append", dest="modes")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--plan-attempts", type=int, default=2)
    parser.add_argument("--transport-attempts", type=int, default=2)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="medium")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def load_cases(case_ids: list[str] | None) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not case_ids:
        return cases
    wanted = set(case_ids)
    selected = [case for case in cases if case["id"] in wanted]
    missing = wanted - {case["id"] for case in selected}
    if missing:
        raise ValueError(f"Unknown cases: {', '.join(sorted(missing))}")
    return selected


def project_paths(workspace: Path) -> set[str]:
    ignored = {"AGENTS.md"}
    return {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".codex" not in path.parts
        and path.name not in ignored
        and "__pycache__" not in path.parts
    }


def project_fingerprints(workspace: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for relative in project_paths(workspace):
        path = workspace / relative
        if path.is_symlink():
            target = str(path.readlink())
            fingerprints[relative] = hashlib.sha256(f"symlink:{target}".encode("utf-8")).hexdigest()
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        fingerprints[relative] = digest.hexdigest()
    return fingerprints


def changed_project_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def validate_runtime(
    case: dict,
    workspace: Path,
    changed_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    return validate_workspace_contract(
        workspace,
        case["allowed_paths"],
        case["validators"],
        changed_paths=changed_paths,
    )


def runtime_prompt(case: dict, mode: str, plan_text: str | None = None, errors: list[str] | None = None) -> str:
    if errors:
        return (
            "Repair the implementation in the current workspace. Change only the allowed project files and run the "
            "relevant tests. Use only these deterministic errors; do not discuss scores or the experiment."
            f"\nALLOWED_PATHS={json.dumps(case['allowed_paths'])}"
            f"\nVALIDATION_ERRORS={json.dumps(errors)}"
            f"\nUSER_REQUEST={case['prompt']}"
            + (f"\nVALIDATED_PLAN={plan_text}" if plan_text else "")
        )
    return (
        ("Use $constraint-exec. " if mode == "full-v2" else "")
        + "Implement the user request in the current workspace. Create or modify only the allowed paths, run the "
        "relevant tests, and leave the working implementation in place. Do not discuss the experiment."
        f"\nALLOWED_PATHS={json.dumps(case['allowed_paths'])}"
        f"\nUSER_REQUEST={case['prompt']}"
        + (f"\nVALIDATED_PLAN={plan_text}" if plan_text else "")
    )


def run_runtime_job(
    case: dict, mode: str, model: str, repeat: int, root: Path,
    effort: str, timeout: int, repair_attempts: int, plan_attempts: int = 2,
    executor_name: str = "codex", transport_attempts: int = 2,
) -> dict[str, Any]:
    variant = VARIANTS["full-v2"] if mode == "full-v2" else VARIANTS["baseline"]
    workspace = prepare_workspace(root, model, repeat, variant, f"runtime-{case['id']}")
    base = Path(slug(model)) / f"r{repeat}" / mode / case["id"]
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    attempts: list[dict[str, Any]] = []
    state = ExecutionState(f"{slug(model)}:r{repeat}:{mode}:{case['id']}")
    if not create_executor(executor_name).capabilities.workspace_access:
        state.finish(False, "executor_capability_mismatch")
        return {
            "case_id": case["id"], "mode": mode, "model": model, "executor": executor_name,
            "repeat": repeat, "success": False, "contract_pass": False,
            "termination_reason": state.termination_reason, "attempts": [], "state": state.to_dict(),
            "plan_retry_count": 0, "artifact_retry_count": 0, "retry_count": 0,
            "transport_retry_count": 0, "usage": usage,
        }
    plan_text = None
    plan_retry_count = 0
    if mode == "full-v2":
        previous = None
        issues: list[dict[str, str]] = []
        for index in range(plan_attempts):
            plan_path = root / "plans" / base.with_suffix(f".a{index + 1}.json")
            trace_path = root / "traces" / "plans" / base.with_suffix(f".a{index + 1}.jsonl")
            call = dispatch_generation(
                plan_prompt({"prompt": case["prompt"]}, variant, previous, issues), model, effort,
                workspace, root / ".codex-homes", plan_path, trace_path, timeout, SCHEMA_PATH,
                executor_name=executor_name, transport_attempts=transport_attempts,
            )
            add_usage(usage, call["usage"])
            attempt = {"phase": "plan", "attempt": index + 1, **call}
            attempts.append(attempt)
            if not call["success"]:
                state.add_attempt(Stage.PLAN, "fail", errors=["PLAN_CALL_FAILED"], evidence={"call": call})
                previous = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
                issues = [{"code": "PLAN_CALL_FAILED", "path": "$"}]
                continue
            previous = plan_path.read_text(encoding="utf-8")
            _parsed, validation = parse_plan(previous, allow_legacy_aliases=False)
            attempt["validation"] = validation.to_dict()
            state.add_attempt(
                Stage.PLAN,
                "pass" if validation.valid else "fail",
                errors=[issue.code for issue in validation.issues],
                evidence={"validation": validation.to_dict()},
            )
            issues = [{"code": issue.code, "path": issue.path} for issue in validation.issues]
            if validation.valid:
                plan_text = previous
                break
        plan_retry_count = max(0, sum(item["phase"] == "plan" for item in attempts) - 1)
        if plan_text is None:
            state.finish(False, "plan_validation_exhausted")
            return {
                "case_id": case["id"], "mode": mode, "model": model, "executor": executor_name, "repeat": repeat,
                "success": False, "contract_pass": False,
                "termination_reason": "plan_validation_exhausted", "attempts": attempts,
                "plan_retry_count": plan_retry_count, "artifact_retry_count": 0,
                "retry_count": plan_retry_count,
                "transport_retry_count": sum(int(item.get("transport_retry_count", 0) or 0) for item in attempts),
                "usage": usage, "state": state.to_dict(),
            }

    output_path = root / "messages" / base.with_suffix(".md")
    trace_path = root / "traces" / "execute" / base.with_suffix(".jsonl")
    before_execute = project_fingerprints(workspace)
    call = dispatch_generation(
        runtime_prompt(case, mode, plan_text), model, effort, workspace, root / ".codex-homes",
        output_path, trace_path, timeout, sandbox="workspace-write",
        executor_name=executor_name, transport_attempts=transport_attempts,
    )
    add_usage(usage, call["usage"])
    execute_changed = changed_project_paths(before_execute, project_fingerprints(workspace))
    attempts.append({"phase": "execute", "changed_paths": execute_changed, **call})
    state.add_attempt(
        Stage.EXECUTE,
        "pass" if call["success"] else "fail",
        errors=[] if call["success"] else ["EXECUTION_CALL_FAILED"],
        changed_paths=execute_changed,
        evidence={"call": call},
    )
    validations = validate_runtime(case, workspace, execute_changed) if call["success"] else []
    state.update_validations(validations)
    errors = [error for result in validations if result["status"] == "fail" for error in result["errors"]]
    unsupported = [result for result in validations if result["status"] == "unsupported"]
    if call["success"]:
        validation_status = "fail" if errors else ("unsupported" if unsupported else "pass")
        state.add_attempt(Stage.VALIDATE, validation_status, errors=errors, evidence={"validations": validations})
    repair_count = 0
    repair_success = False
    if mode == "full-v2":
        for index in range(repair_attempts):
            if not errors:
                break
            repair_count += 1
            repair_output = root / "messages" / "repairs" / base.with_suffix(f".a{index + 1}.md")
            repair_trace = root / "traces" / "repairs" / base.with_suffix(f".a{index + 1}.jsonl")
            before_repair = project_fingerprints(workspace)
            repair = dispatch_generation(
                runtime_prompt(case, mode, plan_text, errors), model, effort, workspace, root / ".codex-homes",
                repair_output, repair_trace, timeout, sandbox="workspace-write",
                executor_name=executor_name, transport_attempts=transport_attempts,
            )
            add_usage(usage, repair["usage"])
            repair_changed = changed_project_paths(before_repair, project_fingerprints(workspace))
            attempts.append({"phase": "repair", "attempt": index + 1, "changed_paths": repair_changed, **repair})
            state.add_attempt(
                Stage.REPAIR,
                "pass" if repair["success"] else "fail",
                errors=[] if repair["success"] else ["REPAIR_CALL_FAILED"],
                changed_paths=repair_changed,
                evidence={"call": repair},
            )
            if repair["success"]:
                validations = validate_runtime(
                    case,
                    workspace,
                    changed_project_paths(before_execute, project_fingerprints(workspace)),
                )
                state.update_validations(validations)
                errors = [error for result in validations if result["status"] == "fail" for error in result["errors"]]
                unsupported = [result for result in validations if result["status"] == "unsupported"]
                validation_status = "fail" if errors else ("unsupported" if unsupported else "pass")
                state.add_attempt(Stage.VALIDATE, validation_status, errors=errors, evidence={"validations": validations})
                if not errors and not unsupported:
                    repair_success = True
                    break
    snapshot: dict[str, str] = {}
    for path in sorted(project_paths(workspace)):
        artifact = workspace / path
        snapshot[path] = (
            f"<symlink:{artifact.readlink()}>"
            if artifact.is_symlink()
            else artifact.read_text(encoding="utf-8", errors="replace")
        )
    snapshot_path = root / "artifacts" / base.with_suffix(".json")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    contract_pass = call["success"] and not errors and not unsupported
    termination_reason = (
        "success" if contract_pass else
        "validation_unsupported" if unsupported else
        "validation_exhausted" if call["success"] else
        "execution_failed"
    )
    state.finish(contract_pass, termination_reason)
    return {
        "case_id": case["id"], "mode": mode, "model": model, "executor": executor_name, "repeat": repeat,
        "success": call["success"], "contract_pass": contract_pass,
        "termination_reason": termination_reason,
        "validations": validations, "errors": errors, "attempts": attempts,
        "retry_count": plan_retry_count + repair_count, "artifact_retry_count": repair_count,
        "transport_retry_count": sum(int(item.get("transport_retry_count", 0) or 0) for item in attempts),
        "plan_retry_count": plan_retry_count, "repair_success": repair_success, "usage": usage,
        "state": state.to_dict(),
    }


def checkpoint(path: Path, metadata: dict, rows: dict[tuple[str, int, str, str], dict]) -> None:
    payload = {**metadata, "results": list(rows.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if args.plan_attempts < 1 or args.repair_attempts < 0 or args.transport_attempts < 1:
        raise ValueError("retry attempt counts must be non-negative and plan attempts must be positive")
    modes = list(dict.fromkeys(args.modes or MODES))
    unknown = set(modes) - set(MODES)
    if unknown:
        raise ValueError(f"Unknown modes: {', '.join(sorted(unknown))}")
    cases = load_cases(args.case_ids)
    root = args.output_root / args.experiment
    results_path = root / "runtime-results.json"
    rows: dict[tuple[str, int, str, str], dict] = {}
    if args.resume and results_path.is_file():
        previous = json.loads(results_path.read_text(encoding="utf-8"))
        if previous.get("executor", "codex") == args.executor:
            rows = {(row["model"], row["repeat"], row["mode"], row["case_id"]): row for row in previous["results"]}
    jobs = []
    for model in args.models:
        for repeat in range(1, args.repeat + 1):
            for mode in modes:
                for case in cases:
                    key = (model, repeat, mode, case["id"])
                    if args.resume and rows.get(key, {}).get("contract_pass"):
                        continue
                    jobs.append((case, mode, model, repeat))
    metadata = {
        "experiment": args.experiment, "protocol": RUNTIME_PROTOCOL,
        "generated_at": datetime.now(timezone.utc).isoformat(), "models": args.models,
        "executor": args.executor, "transport_attempts": args.transport_attempts,
        "modes": modes, "repeats": args.repeat, "cases": [case["id"] for case in cases],
    }
    checkpoint(results_path, metadata, rows)
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_map = {
            executor.submit(run_runtime_job, case, mode, model, repeat, root, args.reasoning_effort,
                            args.timeout, args.repair_attempts, args.plan_attempts,
                            args.executor, args.transport_attempts): (case, mode, model, repeat)
            for case, mode, model, repeat in jobs
        }
        for future in as_completed(future_map):
            case, mode, model, repeat = future_map[future]
            key = (model, repeat, mode, case["id"])
            try:
                rows[key] = future.result()
            except Exception as exc:  # pragma: no cover
                rows[key] = {"case_id": case["id"], "mode": mode, "model": model, "repeat": repeat,
                             "executor": args.executor, "success": False, "contract_pass": False,
                             "error": repr(exc)}
            checkpoint(results_path, metadata, rows)
            print(f"[runtime] {model} r{repeat} {mode} {case['id']} pass={rows[key].get('contract_pass')}", flush=True)
    return 0 if all(row.get("contract_pass") for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
