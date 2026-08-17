"""Run real workspace-write direct versus full-V2 artifact experiments."""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from experiment_variants import VARIANTS
from protocol import parse_plan, validate_markdown_artifact
from run_matrix import (
    DEFAULT_OUTPUT_ROOT,
    SCHEMA_PATH,
    add_usage,
    plan_prompt,
    prepare_workspace,
    run_codex,
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
    parser.add_argument("--mode", action="append", dest="modes")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--plan-attempts", type=int, default=2)
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


def validate_runtime(case: dict, workspace: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    actual_paths = project_paths(workspace)
    allowed_paths = set(case["allowed_paths"])
    unexpected = sorted(actual_paths - allowed_paths)
    results.append({
        "type": "path_scope", "status": "pass" if not unexpected else "fail",
        "errors": [f"PATH_SCOPE:{path}" for path in unexpected],
    })
    for validator in case["validators"]:
        kind = validator["type"]
        errors: list[str] = []
        details: dict[str, Any] = {}
        if kind == "files_exist":
            errors = [f"FILE_MISSING:{path}" for path in validator["paths"] if not (workspace / path).is_file()]
        elif kind == "json_exact":
            path = workspace / validator["path"]
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value != validator["value"]:
                    errors.append(f"JSON_VALUE:{validator['path']}")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"JSON_INVALID:{validator['path']}:{exc}")
        elif kind == "markdown_headings":
            path = workspace / validator["path"]
            if not path.is_file():
                errors.append(f"FILE_MISSING:{validator['path']}")
            else:
                result = validate_markdown_artifact(path.read_text(encoding="utf-8"), validator["headings"])
                errors.extend(f"MARKDOWN:{error}" for error in result.errors)
        elif kind == "python_compile":
            for relative in validator["paths"]:
                path = workspace / relative
                try:
                    compile(path.read_text(encoding="utf-8"), relative, "exec")
                except (OSError, SyntaxError, ValueError) as exc:
                    errors.append(f"PYTHON_COMPILE:{relative}:{exc}")
        elif kind == "forbidden_imports":
            forbidden = set(validator["imports"])
            for relative in validator["paths"]:
                path = workspace / relative
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, SyntaxError) as exc:
                    errors.append(f"PYTHON_AST:{relative}:{exc}")
                    continue
                imports = {
                    alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imports.update(
                    (node.module or "").split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                )
                for name in sorted(imports & forbidden):
                    errors.append(f"FORBIDDEN_IMPORT:{relative}:{name}")
        elif kind == "forbidden_pattern":
            for relative in validator["paths"]:
                path = workspace / relative
                content = path.read_text(encoding="utf-8") if path.is_file() else ""
                for pattern in validator["patterns"]:
                    if re.search(pattern, content):
                        errors.append(f"FORBIDDEN_PATTERN:{relative}")
        elif kind == "command":
            completed = subprocess.run(
                validator["command"], cwd=workspace, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            details = {"returncode": completed.returncode, "output": (completed.stdout + completed.stderr)[-4000:]}
            if completed.returncode:
                errors.append(f"COMMAND_FAILED:{' '.join(validator['command'])}")
        else:
            errors.append(f"VALIDATOR_UNSUPPORTED:{kind}")
        results.append({"type": kind, "status": "pass" if not errors else "fail", "errors": errors, **details})
    return results


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
        ("Use $constraint-aware-task-execution. " if mode == "full-v2" else "")
        + "Implement the user request in the current workspace. Create or modify only the allowed paths, run the "
        "relevant tests, and leave the working implementation in place. Do not discuss the experiment."
        f"\nALLOWED_PATHS={json.dumps(case['allowed_paths'])}"
        f"\nUSER_REQUEST={case['prompt']}"
        + (f"\nVALIDATED_PLAN={plan_text}" if plan_text else "")
    )


def run_runtime_job(
    case: dict, mode: str, model: str, repeat: int, root: Path,
    effort: str, timeout: int, repair_attempts: int, plan_attempts: int = 2,
) -> dict[str, Any]:
    variant = VARIANTS["full-v2"] if mode == "full-v2" else VARIANTS["baseline"]
    workspace = prepare_workspace(root, model, repeat, variant, f"runtime-{case['id']}")
    base = Path(slug(model)) / f"r{repeat}" / mode / case["id"]
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    attempts: list[dict[str, Any]] = []
    plan_text = None
    plan_retry_count = 0
    if mode == "full-v2":
        previous = None
        issues: list[dict[str, str]] = []
        for index in range(plan_attempts):
            plan_path = root / "plans" / base.with_suffix(f".a{index + 1}.json")
            trace_path = root / "traces" / "plans" / base.with_suffix(f".a{index + 1}.jsonl")
            call = run_codex(
                plan_prompt({"prompt": case["prompt"]}, variant, previous, issues), model, effort,
                workspace, root / ".codex-homes", plan_path, trace_path, timeout, SCHEMA_PATH,
            )
            add_usage(usage, call["usage"])
            attempt = {"phase": "plan", "attempt": index + 1, **call}
            attempts.append(attempt)
            if not call["success"]:
                previous = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
                issues = [{"code": "PLAN_CALL_FAILED", "path": "$"}]
                continue
            previous = plan_path.read_text(encoding="utf-8")
            _parsed, validation = parse_plan(previous)
            attempt["validation"] = validation.to_dict()
            issues = [{"code": issue.code, "path": issue.path} for issue in validation.issues]
            if validation.valid:
                plan_text = previous
                break
        plan_retry_count = max(0, sum(item["phase"] == "plan" for item in attempts) - 1)
        if plan_text is None:
            return {
                "case_id": case["id"], "mode": mode, "model": model, "repeat": repeat,
                "success": False, "contract_pass": False,
                "termination_reason": "plan_validation_exhausted", "attempts": attempts,
                "plan_retry_count": plan_retry_count, "artifact_retry_count": 0,
                "retry_count": plan_retry_count, "usage": usage,
            }

    output_path = root / "messages" / base.with_suffix(".md")
    trace_path = root / "traces" / "execute" / base.with_suffix(".jsonl")
    call = run_codex(
        runtime_prompt(case, mode, plan_text), model, effort, workspace, root / ".codex-homes",
        output_path, trace_path, timeout, sandbox="workspace-write",
    )
    add_usage(usage, call["usage"])
    attempts.append({"phase": "execute", **call})
    validations = validate_runtime(case, workspace) if call["success"] else []
    errors = [error for result in validations for error in result["errors"]]
    repair_count = 0
    repair_success = False
    if mode == "full-v2":
        for index in range(repair_attempts):
            if not errors:
                break
            repair_count += 1
            repair_output = root / "messages" / "repairs" / base.with_suffix(f".a{index + 1}.md")
            repair_trace = root / "traces" / "repairs" / base.with_suffix(f".a{index + 1}.jsonl")
            repair = run_codex(
                runtime_prompt(case, mode, plan_text, errors), model, effort, workspace, root / ".codex-homes",
                repair_output, repair_trace, timeout, sandbox="workspace-write",
            )
            add_usage(usage, repair["usage"])
            attempts.append({"phase": "repair", "attempt": index + 1, **repair})
            if repair["success"]:
                validations = validate_runtime(case, workspace)
                errors = [error for result in validations for error in result["errors"]]
                if not errors:
                    repair_success = True
                    break
    snapshot = {
        path: (workspace / path).read_text(encoding="utf-8", errors="replace")
        for path in sorted(project_paths(workspace))
    }
    snapshot_path = root / "artifacts" / base.with_suffix(".json")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "case_id": case["id"], "mode": mode, "model": model, "repeat": repeat,
        "success": call["success"], "contract_pass": not errors,
        "termination_reason": "success" if not errors else "validation_exhausted",
        "validations": validations, "errors": errors, "attempts": attempts,
        "retry_count": plan_retry_count + repair_count, "artifact_retry_count": repair_count,
        "plan_retry_count": plan_retry_count, "repair_success": repair_success, "usage": usage,
    }


def checkpoint(path: Path, metadata: dict, rows: dict[tuple[str, int, str, str], dict]) -> None:
    payload = {**metadata, "results": list(rows.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if args.plan_attempts < 1 or args.repair_attempts < 0:
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
        "modes": modes, "repeats": args.repeat, "cases": [case["id"] for case in cases],
    }
    checkpoint(results_path, metadata, rows)
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_map = {
            executor.submit(run_runtime_job, case, mode, model, repeat, root, args.reasoning_effort,
                            args.timeout, args.repair_attempts, args.plan_attempts): (case, mode, model, repeat)
            for case, mode, model, repeat in jobs
        }
        for future in as_completed(future_map):
            case, mode, model, repeat = future_map[future]
            key = (model, repeat, mode, case["id"])
            try:
                rows[key] = future.result()
            except Exception as exc:  # pragma: no cover
                rows[key] = {"case_id": case["id"], "mode": mode, "model": model, "repeat": repeat,
                             "success": False, "contract_pass": False, "error": repr(exc)}
            checkpoint(results_path, metadata, rows)
            print(f"[runtime] {model} r{repeat} {mode} {case['id']} pass={rows[key].get('contract_pass')}", flush=True)
    return 0 if all(row.get("contract_pass") for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
