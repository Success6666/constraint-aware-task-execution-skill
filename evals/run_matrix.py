"""Run orthogonal V1/V2 experiment variants with real plan validation and repair."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from execution_state import ExecutionState, Stage
from executors import GenerationRequest, attempt_summary, create_executor, generate_with_transport_retries
from experiment_variants import VARIANTS, Variant, select_variants
from protocol import choose_retry, parse_plan
from scorer import score_response


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "cases.json"
SCHEMA_PATH = ROOT / "evals" / "schemas" / "execution-plan.schema.json"
SKILL_PATH = ROOT / "skills" / "constraint-aware-task-execution" / "SKILL.md"
PROTOCOL_PATH = ROOT / "evals" / "protocol.py"
DEFAULT_OUTPUT_ROOT = ROOT / "evals" / "experiments"
RUNNER_PROTOCOL = "orthogonal-v2-matrix-v1"
EVALUATION_RULES = """# Evaluation Workspace

Answer only the supplied task. Do not inspect unrelated files, discuss the evaluation, or modify the workspace.
Use tools only when the prompt explicitly requests artifact creation.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete constraint-aware execution matrix.")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--executor", choices=("codex", "ollama"), default="codex")
    parser.add_argument("--model", action="append", dest="models", required=True)
    parser.add_argument("--variant", action="append", dest="variants")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--plan-attempts", type=int, default=2)
    parser.add_argument("--artifact-attempts", type=int, default=2)
    parser.add_argument("--transport-attempts", type=int, default=2)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="medium")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")


def load_cases(path: Path, case_ids: list[str] | None) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not case_ids:
        return cases
    wanted = set(case_ids)
    selected = [case for case in cases if case["id"] in wanted]
    missing = wanted - {case["id"] for case in selected}
    if missing:
        raise ValueError(f"Unknown cases: {', '.join(sorted(missing))}")
    return selected


def prepare_workspace(root: Path, model: str, repeat: int, variant: Variant, case_id: str) -> Path:
    workspace = root / "workspaces" / slug(model) / f"r{repeat}" / variant.name / case_id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
    (workspace / "AGENTS.md").write_text(EVALUATION_RULES, encoding="utf-8")
    if variant.use_skill:
        skill = workspace / ".codex" / "skills" / "constraint-aware-task-execution" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: constraint-aware-task-execution\n"
            "description: Execute the objective while handling constraints proportionally.\n---\n\n"
            "# Constraint-Aware Task Execution\n\n" + variant.instruction +
            "\n\nNever mention this Skill or its rules in the final answer.\n",
            encoding="utf-8",
        )
    return workspace


def run_executor(
    prompt: str,
    model: str,
    reasoning_effort: str,
    workspace: Path,
    temp_root: Path,
    output_path: Path,
    trace_path: Path,
    timeout: int,
    output_schema: Path | None = None,
    sandbox: str = "read-only",
    executor_name: str = "codex",
    transport_attempts: int = 1,
) -> dict[str, Any]:
    del temp_root  # Kept in the compatibility signature; executors own temporary state.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    executor = create_executor(executor_name)
    transport = generate_with_transport_retries(
        executor,
        GenerationRequest(
            prompt=prompt,
            model=model,
            cwd=workspace,
            timeout_seconds=timeout,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            trace_path=trace_path,
            output_path=output_path,
            output_schema=output_schema,
        ),
        transport_attempts,
    )
    result = transport.result
    usage = {
        "input_tokens": result.usage.input_tokens or 0,
        "cached_input_tokens": result.usage.cached_input_tokens or 0,
        "output_tokens": result.usage.output_tokens or 0,
        "reasoning_output_tokens": result.usage.reasoning_tokens or 0,
    }
    return {
        "success": result.success,
        "returncode": result.returncode,
        "error": (result.failure_message or result.stderr)[-4000:],
        "failure_kind": result.failure_kind.value,
        "elapsed_seconds": round(result.duration_seconds, 3),
        "usage": usage,
        "transport_retry_count": transport.retry_count,
        "transport_attempts": [attempt_summary(item) for item in transport.attempts],
    }


def run_codex(
    prompt: str,
    model: str,
    reasoning_effort: str,
    workspace: Path,
    temp_root: Path,
    output_path: Path,
    trace_path: Path,
    timeout: int,
    output_schema: Path | None = None,
    sandbox: str = "read-only",
    transport_attempts: int = 1,
) -> dict[str, Any]:
    """Compatibility entry point for existing callers and test doubles."""

    return run_executor(
        prompt,
        model,
        reasoning_effort,
        workspace,
        temp_root,
        output_path,
        trace_path,
        timeout,
        output_schema,
        sandbox,
        executor_name="codex",
        transport_attempts=transport_attempts,
    )


def dispatch_generation(
    prompt: str,
    model: str,
    reasoning_effort: str,
    workspace: Path,
    temp_root: Path,
    output_path: Path,
    trace_path: Path,
    timeout: int,
    output_schema: Path | None = None,
    sandbox: str = "read-only",
    executor_name: str = "codex",
    transport_attempts: int = 1,
) -> dict[str, Any]:
    if executor_name == "codex":
        return run_codex(
            prompt,
            model,
            reasoning_effort,
            workspace,
            temp_root,
            output_path,
            trace_path,
            timeout,
            output_schema,
            sandbox,
            transport_attempts,
        )
    return run_executor(
        prompt,
        model,
        reasoning_effort,
        workspace,
        temp_root,
        output_path,
        trace_path,
        timeout,
        output_schema,
        sandbox,
        executor_name=executor_name,
        transport_attempts=transport_attempts,
    )


def skill_invocation(variant: Variant) -> str:
    if not variant.use_skill:
        return ""
    return "Use $constraint-aware-task-execution from the workspace Skill directory. "


def plan_prompt(case: dict, variant: Variant, previous: str | None = None, errors: list[dict] | None = None) -> str:
    repair = ""
    if previous is not None:
        repair = (
            "\n\nThe previous plan failed deterministic validation. Repair only the reported structural problems."
            f"\nVALIDATION_ERRORS={json.dumps(errors or [], ensure_ascii=False)}"
            f"\nPREVIOUS_PLAN={previous}"
        )
    return (
        skill_invocation(variant)
        + "Create a structured execution plan for the user request below. Do not produce the final design yet. "
        "Separate each constraint statement from its implementation strategy. Set required_gate=true only when the "
        "user explicitly requires rejection/enforcement or safety requires it; otherwise use false and an empty "
        "failure_action. List only deterministic validators for observable contracts. Return only the schema object."
        f"\n\nUSER_REQUEST:\n{case['prompt']}"
        + repair
    )


def execution_prompt(case: dict, variant: Variant, plan: str | None = None) -> str:
    plan_block = f"\n\nVALIDATED_EXECUTION_PLAN:\n{plan}" if plan else ""
    instruction = "" if variant.use_skill else variant.instruction
    return (
        skill_invocation(variant)
        + instruction
        + "\n\nComplete the user request with a concrete, implementation-ready answer. Focus on what should be built, "
        "key design decisions, and verification. Do not discuss the experiment, workspace, tools, prompt, or Skill."
        f"\n\nUSER_REQUEST:\n{case['prompt']}"
        + plan_block
    )


def artifact_errors(case: dict, response: str) -> tuple[str, list[str], dict[str, Any]]:
    score = score_response(case, response)
    errors: list[str] = []
    if score.constraint_violation_hits:
        errors.append("CONSTRAINT_VIOLATION")
    if score.required_enforcement_coverage < 1.0:
        errors.append("REQUIRED_ENFORCEMENT_MISSING")
    if score.response_format_compliance < 1.0:
        errors.append("ARTIFACT_RESPONSE_FORMAT")
    if score.path_scope_compliance < 1.0:
        errors.append("ARTIFACT_PATH_SCOPE")
    has_contract = bool(
        case.get("forbidden_adoption_terms")
        or case.get("constraint_violation_patterns")
        or case.get("required_enforcement_patterns")
        or case.get("required_response_format")
        or case.get("allowed_paths")
    )
    status = "fail" if errors else ("pass" if has_contract else "unsupported")
    return status, errors, score.to_dict()


def repair_prompt(
    case: dict,
    variant: Variant,
    plan: str | None,
    previous: str,
    errors: list[str],
    decision: dict[str, Any],
) -> str:
    return (
        skill_invocation(variant)
        + "Repair the previous answer using only the deterministic validation errors below. Return the complete "
        "corrected answer, with no discussion of validation scores or the experiment."
        f"\n\nUSER_REQUEST:\n{case['prompt']}"
        + (f"\n\nVALIDATED_EXECUTION_PLAN:\n{plan}" if plan else "")
        + f"\n\nVALIDATION_ERRORS={json.dumps(errors)}"
        + f"\nREPAIR_ACTION={decision['next_action']}"
        + f"\n\nPREVIOUS_ANSWER:\n{previous}"
    )


def signature(
    case: dict,
    variant: Variant,
    model: str,
    repeat: int,
    effort: str,
    executor_name: str = "codex",
    transport_attempts: int = 1,
) -> str:
    payload = {
        "case": case,
        "variant": asdict(variant),
        "model": model,
        "repeat": repeat,
        "reasoning_effort": effort,
        "executor": executor_name,
        "transport_attempts": transport_attempts,
        "runner_protocol": RUNNER_PROTOCOL,
        "plan_schema": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "protocol_digest": hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest(),
        "skill_digest": hashlib.sha256(SKILL_PATH.read_bytes()).hexdigest() if variant.use_skill else None,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def sampling_signature(model: str, effort: str, executor_name: str, transport_attempts: int) -> str:
    payload = {
        "model": model,
        "reasoning_effort": effort,
        "executor": executor_name,
        "transport_attempts": transport_attempts,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def add_usage(total: dict[str, int], addition: dict[str, int]) -> None:
    for key, value in addition.items():
        total[key] = total.get(key, 0) + value


def run_job(
    case: dict,
    variant: Variant,
    model: str,
    repeat: int,
    experiment_root: Path,
    effort: str,
    timeout: int,
    plan_attempts: int,
    artifact_attempts: int,
    executor_name: str = "codex",
    transport_attempts: int = 1,
) -> dict[str, Any]:
    workspace = prepare_workspace(experiment_root, model, repeat, variant, case["id"])
    base = Path(slug(model)) / f"r{repeat}" / variant.name / case["id"]
    raw_root = experiment_root / "raw"
    trace_root = experiment_root / "traces"
    temp_root = experiment_root / ".codex-homes"
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    stages: list[dict[str, Any]] = []
    state = ExecutionState(f"{executor_name}:{slug(model)}:r{repeat}:{variant.name}:{case['id']}")
    plan_text: str | None = None
    plan_result: dict[str, Any] | None = None
    selected_plan_path: Path | None = None

    if variant.structured_plan:
        previous = None
        issues: list[dict] = []
        attempts = plan_attempts if variant.validate_plan else 1
        for attempt in range(attempts):
            plan_path = raw_root / "plans" / base.with_suffix(f".a{attempt + 1}.json")
            trace_path = trace_root / "plans" / base.with_suffix(f".a{attempt + 1}.jsonl")
            call = dispatch_generation(
                plan_prompt(case, variant, previous, issues), model, effort, workspace, temp_root,
                plan_path, trace_path, timeout, SCHEMA_PATH, executor_name=executor_name,
                transport_attempts=transport_attempts,
            )
            add_usage(usage, call["usage"])
            stage = {"stage": "plan", "attempt": attempt + 1, **call}
            stages.append(stage)
            if not call["success"]:
                state.add_attempt(Stage.PLAN, "fail", errors=["PLAN_CALL_FAILED"], evidence={"call": call})
                previous = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
                issues = [{"code": "PLAN_CALL_FAILED", "path": "$", "message": call["error"][-500:]}]
                continue
            previous = plan_path.read_text(encoding="utf-8")
            parsed, validation = parse_plan(previous)
            issues = [{"code": issue.code, "path": issue.path} for issue in validation.issues]
            stage["validation"] = validation.to_dict()
            state.add_attempt(
                Stage.PLAN,
                "pass" if validation.valid else "fail",
                errors=[issue.code for issue in validation.issues],
                evidence={"validation": validation.to_dict()},
            )
            if not variant.validate_plan or validation.valid:
                plan_text = previous
                plan_result = parsed
                selected_plan_path = plan_path
                break
        if plan_text is None:
            state.finish(False, "plan_validation_exhausted")
            return {
                "case_id": case["id"], "variant": variant.name, "model": model, "repeat": repeat,
                "executor": executor_name,
                "sampling_signature": sampling_signature(model, effort, executor_name, transport_attempts),
                "success": False, "failure_stage": "plan", "stages": stages, "usage": usage,
                "transport_retry_count": sum(int(stage.get("transport_retry_count", 0) or 0) for stage in stages),
                "state": state.to_dict(), "termination_reason": state.termination_reason,
            }

    answer_path = raw_root / "answers" / base.with_suffix(".md")
    answer_trace = trace_root / "answers" / base.with_suffix(".jsonl")
    call = dispatch_generation(
        execution_prompt(case, variant, plan_text), model, effort, workspace, temp_root,
        answer_path, answer_trace, timeout, executor_name=executor_name,
        transport_attempts=transport_attempts,
    )
    add_usage(usage, call["usage"])
    stages.append({"stage": "execute", "attempt": 1, **call})
    state.add_attempt(
        Stage.EXECUTE,
        "pass" if call["success"] else "fail",
        errors=[] if call["success"] else ["EXECUTION_CALL_FAILED"],
        evidence={"call": call},
    )
    if not call["success"]:
        state.finish(False, "execution_failed")
        return {
            "case_id": case["id"], "variant": variant.name, "model": model, "repeat": repeat,
            "executor": executor_name,
            "sampling_signature": sampling_signature(model, effort, executor_name, transport_attempts),
            "success": False, "failure_stage": "execute", "stages": stages, "usage": usage,
            "transport_retry_count": sum(int(stage.get("transport_retry_count", 0) or 0) for stage in stages),
            "state": state.to_dict(), "termination_reason": state.termination_reason,
        }
    response = answer_path.read_text(encoding="utf-8")
    artifact_status, errors, score = artifact_errors(case, response)
    state.add_attempt(Stage.VALIDATE, artifact_status, errors=errors, evidence={"score": score})
    retry_events: list[dict[str, Any]] = []

    if variant.repair_artifact:
        for attempt in range(artifact_attempts):
            if not errors:
                break
            decision = choose_retry(errors, attempt, artifact_attempts).to_dict()
            retry_events.append({"attempt": attempt + 1, "errors": errors, "decision": decision})
            if decision["level"] == "stop":
                break
            repair_path = raw_root / "repairs" / base.with_suffix(f".a{attempt + 1}.md")
            repair_trace = trace_root / "repairs" / base.with_suffix(f".a{attempt + 1}.jsonl")
            repair = dispatch_generation(
                repair_prompt(case, variant, plan_text, response, errors, decision), model, effort,
                workspace, temp_root, repair_path, repair_trace, timeout,
                executor_name=executor_name,
                transport_attempts=transport_attempts,
            )
            add_usage(usage, repair["usage"])
            stages.append({"stage": "repair", "attempt": attempt + 1, **repair})
            state.add_attempt(
                Stage.REPAIR,
                "pass" if repair["success"] else "fail",
                errors=[] if repair["success"] else ["REPAIR_CALL_FAILED"],
                evidence={"call": repair},
            )
            if not repair["success"]:
                continue
            response = repair_path.read_text(encoding="utf-8")
            answer_path.write_text(response, encoding="utf-8")
            artifact_status, errors, score = artifact_errors(case, response)
            state.add_attempt(Stage.VALIDATE, artifact_status, errors=errors, evidence={"score": score})

    success = artifact_status != "fail"
    contract_pass = True if artifact_status == "pass" else (False if artifact_status == "fail" else None)
    termination_reason = "success" if artifact_status == "pass" else ("unsupported" if artifact_status == "unsupported" else "artifact_validation_exhausted")
    state.finish(success, termination_reason)
    return {
        "case_id": case["id"],
        "variant": variant.name,
        "model": model,
        "executor": executor_name,
        "sampling_signature": sampling_signature(model, effort, executor_name, transport_attempts),
        "repeat": repeat,
        "success": success,
        "failure_stage": None if success else "artifact",
        "signature": signature(case, variant, model, repeat, effort, executor_name, transport_attempts),
        "plan": plan_result,
        "artifact_errors": errors,
        "artifact_validation_status": artifact_status,
        "artifact_contract_pass": contract_pass,
        "retry_events": retry_events,
        "retry_count": len(retry_events),
        "transport_retry_count": sum(int(stage.get("transport_retry_count", 0) or 0) for stage in stages),
        "plan_retry_count": max(0, sum(stage["stage"] == "plan" for stage in stages) - 1),
        "artifact_retry_count": sum(stage["stage"] == "repair" for stage in stages),
        "repair_success": bool(retry_events) and artifact_status == "pass",
        "termination_reason": termination_reason,
        "stages": stages,
        "state": state.to_dict(),
        "usage": usage,
        "score": score,
        "evidence": {
            "answer": answer_path.relative_to(experiment_root).as_posix(),
            **({"plan": selected_plan_path.relative_to(experiment_root).as_posix()} if selected_plan_path else {}),
        },
    }


def evidence_exists(experiment_root: Path, row: dict[str, Any], variant: Variant) -> bool:
    evidence = row.get("evidence")
    if not isinstance(evidence, dict) or not evidence.get("answer"):
        return False
    required = [evidence["answer"]]
    if variant.structured_plan:
        if not evidence.get("plan"):
            return False
        required.append(evidence["plan"])
    return all((experiment_root / relative).is_file() for relative in required)


def write_checkpoint(path: Path, rows: dict[tuple[str, int, str, str], dict], metadata: dict) -> None:
    payload = {**metadata, "results": list(rows.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if args.jobs < 1 or args.jobs > 8:
        raise ValueError("--jobs must be between 1 and 8")
    if args.repeat < 1:
        raise ValueError("--repeat must be positive")
    if args.transport_attempts < 1:
        raise ValueError("--transport-attempts must be positive")
    cases = load_cases(args.cases, args.case_ids)
    variants = select_variants(args.variants)
    experiment_root = args.output_root / args.experiment
    results_path = experiment_root / "results.json"
    existing: dict[tuple[str, int, str, str], dict] = {}
    if args.resume and results_path.is_file():
        payload = json.loads(results_path.read_text(encoding="utf-8"))
        existing = {
            (row["model"], row["repeat"], row["variant"], row["case_id"]): row
            for row in payload.get("results", [])
        }

    jobs: list[tuple[dict, Variant, str, int]] = []
    rows = dict(existing)
    for model in args.models:
        for repeat in range(1, args.repeat + 1):
            for variant in variants:
                for case in cases:
                    key = (model, repeat, variant.name, case["id"])
                    previous = rows.get(key)
                    expected = signature(
                        case, variant, model, repeat, args.reasoning_effort, args.executor,
                        args.transport_attempts,
                    )
                    if (
                        args.resume and not args.force and previous and previous.get("success")
                        and previous.get("signature") == expected
                        and evidence_exists(experiment_root, previous, variant)
                    ):
                        continue
                    jobs.append((case, variant, model, repeat))

    metadata = {
        "experiment": args.experiment,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner_protocol": RUNNER_PROTOCOL,
        "reasoning_effort": args.reasoning_effort,
        "executor": args.executor,
        "transport_attempts": args.transport_attempts,
        "models": args.models,
        "variants": [variant.name for variant in variants],
        "repeats": args.repeat,
        "cases": [case["id"] for case in cases],
    }
    write_checkpoint(results_path, rows, metadata)
    print(f"Running {len(jobs)} jobs in {experiment_root}", flush=True)

    def execute(job: tuple[dict, Variant, str, int]) -> dict[str, Any]:
        case, variant, model, repeat = job
        print(f"[{model} r{repeat} {variant.name}] {case['id']}", flush=True)
        return run_job(
            case, variant, model, repeat, experiment_root, args.reasoning_effort,
            args.timeout, args.plan_attempts, args.artifact_attempts, args.executor,
            args.transport_attempts,
        )

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_map = {executor.submit(execute, job): job for job in jobs}
        for future in as_completed(future_map):
            case, variant, model, repeat = future_map[future]
            key = (model, repeat, variant.name, case["id"])
            try:
                rows[key] = future.result()
            except Exception as exc:  # pragma: no cover - process boundary
                rows[key] = {
                    "case_id": case["id"], "variant": variant.name, "model": model, "repeat": repeat,
                    "executor": args.executor,
                    "success": False, "failure_stage": "runner", "error": repr(exc),
                }
            write_checkpoint(results_path, rows, metadata)
            print(f"[done] {model} r{repeat} {variant.name} {case['id']} success={rows[key].get('success')}", flush=True)
    return 0 if all(row.get("success") for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
