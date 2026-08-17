"""Run direct versus full-V2 experiments through validated artifact bundles."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import tempfile
import time
from typing import Any

from execution_state import ExecutionState, Stage
from executors import create_executor, execution_runtime_policy
from experiment_variants import VARIANTS
from protocol import (
    merge_plan_validations,
    normalize_plan_validation_issues,
    parse_plan,
    synthesize_minimal_plan,
    validate_plan_context,
)
from validators import validate_workspace_contract
from run_matrix import (
    DEFAULT_OUTPUT_ROOT,
    OUTPUT_SCHEMA_PATH,
    add_usage,
    plan_prompt,
    prepare_workspace,
    dispatch_generation as matrix_dispatch_generation,
    run_codex,
    slug,
)


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "runtime_cases.json"
ARTIFACT_BUNDLE_SCHEMA_PATH = ROOT / "evals" / "schemas" / "artifact-bundle.output.schema.json"
RUNTIME_PROTOCOL = "workspace-artifact-bundle-v1"
MODES = ("direct", "full-v2")
MAX_ARTIFACT_FILE_BYTES = 512 * 1024
MAX_ARTIFACT_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BUNDLE_DOCUMENT_BYTES = 8 * 1024 * 1024
RUNTIME_WORKSPACE_RULES = """# Runtime Inspection Workspace

The workspace contains no task implementation files. Do not call tools or inspect or modify the workspace.
Return only the structured response requested by the current prompt and output schema.
Do not discuss the evaluation.
"""


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
    """Keep the historical run_runtime.run_codex replacement seam."""

    if executor_name == "codex":
        try:
            return run_codex(
                prompt, model, reasoning_effort, workspace, temp_root, output_path,
                trace_path, timeout, output_schema, sandbox=sandbox,
                transport_attempts=transport_attempts,
            )
        except TypeError as error:
            if "unexpected keyword argument" not in str(error):
                raise
            return run_codex(
                prompt, model, reasoning_effort, workspace, temp_root, output_path,
                trace_path, timeout, output_schema, sandbox=sandbox,
            )
    return matrix_dispatch_generation(
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
        executor_name,
        transport_attempts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic workspace artifact experiments.")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--model", action="append", dest="models", required=True)
    parser.add_argument("--executor", choices=("codex", "ollama"), default="codex")
    parser.add_argument("--mode", action="append", dest="modes")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--plan-attempts", type=int, default=2)
    parser.add_argument("--transport-attempts", type=int, default=2)
    parser.add_argument("--inter-stage-delay", type=float, default=0.0)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="medium")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def load_cases(case_ids: list[str] | None, path: Path = CASES_PATH) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError(f"cases file must contain an array: {path}")
    if not case_ids:
        return cases
    wanted = set(case_ids)
    selected = [case for case in cases if case["id"] in wanted]
    missing = wanted - {case["id"] for case in selected}
    if missing:
        raise ValueError(f"Unknown cases: {', '.join(sorted(missing))}")
    return selected


def artifact_bundle_metadata() -> dict[str, Any]:
    return {
        "schema_sha256": hashlib.sha256(ARTIFACT_BUNDLE_SCHEMA_PATH.read_bytes()).hexdigest(),
        "max_file_bytes": MAX_ARTIFACT_FILE_BYTES,
        "max_bundle_bytes": MAX_ARTIFACT_BUNDLE_BYTES,
        "max_document_bytes": MAX_ARTIFACT_BUNDLE_DOCUMENT_BYTES,
        "max_files": 64,
    }


def runtime_signature(
    case: dict,
    mode: str,
    model: str,
    repeat: int,
    effort: str,
    executor_name: str = "codex",
    transport_attempts: int = 1,
    inter_stage_delay: float = 0.0,
) -> str:
    payload = {
        "case": case,
        "mode": mode,
        "model": model,
        "repeat": repeat,
        "reasoning_effort": effort,
        "executor": executor_name,
        "transport_attempts": transport_attempts,
        "execution_runtime_policy": execution_runtime_policy(executor_name),
        "inter_stage_delay": inter_stage_delay,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifact_bundle": artifact_bundle_metadata(),
        "plan_output_schema": hashlib.sha256(OUTPUT_SCHEMA_PATH.read_bytes()).hexdigest(),
        "skill_digest": (
            hashlib.sha256((ROOT / "skills" / "constraint-exec" / "SKILL.md").read_bytes()).hexdigest()
            if mode == "full-v2"
            else None
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_artifact_path(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None, "ARTIFACT_PATH_INVALID"
    if "\x00" in value or "\\" in value or ":" in value:
        return None, f"ARTIFACT_PATH_INVALID:{value}"
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None, f"ARTIFACT_PATH_INVALID:{value}"
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return None, f"ARTIFACT_PATH_INVALID:{value}"
    return value, None


def _safe_artifact_target(workspace: Path, relative: str) -> tuple[Path | None, str | None]:
    root = workspace.resolve()
    target = workspace.joinpath(*PurePosixPath(relative).parts)
    current = workspace
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            return None, f"ARTIFACT_SYMLINK:{relative}"
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        return None, f"ARTIFACT_PATH_INVALID:{relative}"
    if resolved != root and root not in resolved.parents:
        return None, f"ARTIFACT_PATH_ESCAPE:{relative}"
    return target, None


def apply_artifact_bundle(
    bundle_path: Path,
    workspace: Path,
    allowed_paths: list[str],
) -> tuple[list[str], list[str]]:
    if not bundle_path.is_file():
        return [], ["ARTIFACT_BUNDLE_MISSING"]
    try:
        if bundle_path.stat().st_size > MAX_ARTIFACT_BUNDLE_DOCUMENT_BYTES:
            return [], ["ARTIFACT_BUNDLE_TOO_LARGE"]
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], ["ARTIFACT_BUNDLE_JSON"]
    if not isinstance(payload, dict) or set(payload) != {"files"}:
        return [], ["ARTIFACT_BUNDLE_SHAPE"]
    files = payload.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 64:
        return [], ["ARTIFACT_BUNDLE_SHAPE"]

    normalized_allowed: set[str] = set()
    for allowed in allowed_paths:
        normalized, error = _normalize_artifact_path(allowed)
        if error or normalized is None:
            return [], [f"ALLOWED_PATH_INVALID:{allowed}"]
        normalized_allowed.add(normalized)

    prepared: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    errors: list[str] = []
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            errors.append("ARTIFACT_BUNDLE_SHAPE")
            continue
        relative, path_error = _normalize_artifact_path(item.get("path"))
        if path_error or relative is None:
            errors.append(path_error or "ARTIFACT_PATH_INVALID")
            continue
        collision_key = relative.casefold()
        if collision_key in seen:
            errors.append(f"ARTIFACT_PATH_DUPLICATE:{relative}")
            continue
        seen.add(collision_key)
        if relative not in normalized_allowed:
            errors.append(f"ARTIFACT_PATH_NOT_ALLOWED:{relative}")
            continue
        content = item.get("content")
        if not isinstance(content, str):
            errors.append(f"ARTIFACT_CONTENT_TYPE:{relative}")
            continue
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            errors.append(f"ARTIFACT_CONTENT_ENCODING:{relative}")
            continue
        if content_bytes > MAX_ARTIFACT_FILE_BYTES:
            errors.append(f"ARTIFACT_FILE_TOO_LARGE:{relative}")
            continue
        total_bytes += content_bytes
        target, target_error = _safe_artifact_target(workspace, relative)
        if target_error or target is None:
            errors.append(target_error or f"ARTIFACT_PATH_INVALID:{relative}")
            continue
        prepared.append((relative, content, target))
    if total_bytes > MAX_ARTIFACT_BUNDLE_BYTES:
        errors.append("ARTIFACT_BUNDLE_TOO_LARGE")
    if errors:
        return [], list(dict.fromkeys(errors))

    staged: list[tuple[str, Path, Path]] = []
    try:
        for relative, content, target in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            checked_target, target_error = _safe_artifact_target(workspace, relative)
            if target_error or checked_target != target:
                raise ValueError(target_error or f"ARTIFACT_PATH_INVALID:{relative}")
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            staged.append((relative, temporary, target))
        for relative, temporary, target in staged:
            checked_target, target_error = _safe_artifact_target(workspace, relative)
            if target_error or checked_target != target:
                raise ValueError(target_error or f"ARTIFACT_PATH_INVALID:{relative}")
            os.replace(temporary, target)
    except (OSError, ValueError) as error:
        for _relative, temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        message = str(error)
        if message.startswith("ARTIFACT_"):
            return [], [message]
        return [], ["ARTIFACT_WRITE_FAILED"]
    return [relative for relative, _content, _target in prepared], []


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
    bundle_contract = (
        "Return only one JSON object matching the artifact-bundle schema: "
        "{\"files\":[{\"path\":\"relative/allowed/path\",\"content\":\"complete UTF-8 file content\"}]}. "
        "Use POSIX relative paths. Include complete replacement content for each file in the bundle. "
        "Do not call tools or inspect or modify the workspace; it contains no task implementation files. "
        "Generate the complete bundle directly from USER_REQUEST. The runner will validate and write the bundle."
    )
    if errors:
        return (
            "Repair the implementation directly from USER_REQUEST and the deterministic errors. "
            + bundle_contract
            + " Use only these deterministic errors; do not discuss scores or the experiment."
            f"\nALLOWED_PATHS={json.dumps(case['allowed_paths'])}"
            f"\nVALIDATION_ERRORS={json.dumps(errors)}"
            f"\nUSER_REQUEST={case['prompt']}"
            + (f"\nVALIDATED_PLAN={plan_text}" if plan_text else "")
        )
    return (
        ("Use $constraint-exec. " if mode == "full-v2" else "")
        + "Implement the user request directly without workspace inspection. "
        + bundle_contract
        + " Do not discuss the experiment."
        f"\nALLOWED_PATHS={json.dumps(case['allowed_paths'])}"
        f"\nUSER_REQUEST={case['prompt']}"
        + (f"\nVALIDATED_PLAN={plan_text}" if plan_text else "")
    )


def run_runtime_job(
    case: dict, mode: str, model: str, repeat: int, root: Path,
    effort: str, timeout: int, repair_attempts: int, plan_attempts: int = 2,
    executor_name: str = "codex", transport_attempts: int = 2,
    inter_stage_delay: float = 0.0,
) -> dict[str, Any]:
    variant = VARIANTS["full-v2"] if mode == "full-v2" else VARIANTS["baseline"]
    workspace = prepare_workspace(
        root,
        model,
        repeat,
        variant,
        f"runtime-{case['id']}",
        workspace_rules=RUNTIME_WORKSPACE_RULES,
    )
    base = Path(slug(model)) / f"r{repeat}" / mode / case["id"]
    job_signature = runtime_signature(
        case,
        mode,
        model,
        repeat,
        effort,
        executor_name,
        transport_attempts,
        inter_stage_delay,
    )
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    attempts: list[dict[str, Any]] = []
    state = ExecutionState(f"{slug(model)}:r{repeat}:{mode}:{case['id']}")
    if not create_executor(executor_name).capabilities.structured_output:
        state.finish(False, "executor_capability_mismatch")
        return {
            "case_id": case["id"], "mode": mode, "model": model, "executor": executor_name,
            "repeat": repeat, "success": False, "contract_pass": False,
            "termination_reason": state.termination_reason, "attempts": [], "state": state.to_dict(),
            "plan_retry_count": 0, "artifact_retry_count": 0, "retry_count": 0,
            "transport_retry_count": 0, "usage": usage, "signature": job_signature,
        }
    plan_text = None
    plan_retry_count = 0
    plan_fallback_used = False
    if mode == "full-v2":
        previous = None
        issues: list[dict[str, str]] = []
        last_parsed: dict[str, Any] | None = None
        for index in range(plan_attempts):
            plan_path = root / "plans" / base.with_suffix(f".a{index + 1}.json")
            trace_path = root / "traces" / "plans" / base.with_suffix(f".a{index + 1}.jsonl")
            call = dispatch_generation(
                plan_prompt({"prompt": case["prompt"]}, variant, previous, issues), model, effort,
                workspace, root / ".codex-homes", plan_path, trace_path, timeout, OUTPUT_SCHEMA_PATH,
                executor_name=executor_name, transport_attempts=transport_attempts,
            )
            add_usage(usage, call["usage"])
            attempt = {"phase": "plan", "attempt": index + 1, **call}
            attempts.append(attempt)
            if not call["success"]:
                state.add_attempt(Stage.PLAN, "fail", errors=["PLAN_CALL_FAILED"], evidence={"call": call})
                previous = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
                issues = [{"code": "PLAN_CALL_FAILED", "path": "$"}]
                if inter_stage_delay > 0 and index + 1 < plan_attempts:
                    time.sleep(inter_stage_delay)
                continue
            previous = plan_path.read_text(encoding="utf-8")
            parsed, structural_validation = parse_plan(previous, allow_legacy_aliases=False)
            if parsed is not None:
                last_parsed = parsed
            contextual_validation = validate_plan_context(
                parsed or {},
                case.get("constraint_terms", []),
                required_gates_allowed=bool(
                    case.get("required_gate") or case.get("required_enforcement_patterns")
                ),
                soft_preference_only=bool(case.get("soft_preference")),
            )
            validation = merge_plan_validations(structural_validation, contextual_validation)
            normalized_changes: tuple[str, ...] = ()
            if parsed is not None and structural_validation.valid and not validation.valid:
                normalized, normalized_changes = normalize_plan_validation_issues(parsed, validation)
                if normalized_changes:
                    normalized_structural = parse_plan(
                        json.dumps(normalized, ensure_ascii=False), allow_legacy_aliases=False
                    )[1]
                    normalized_contextual = validate_plan_context(
                        normalized,
                        case.get("constraint_terms", []),
                        required_gates_allowed=bool(
                            case.get("required_gate") or case.get("required_enforcement_patterns")
                        ),
                        soft_preference_only=bool(case.get("soft_preference")),
                    )
                    normalized_validation = merge_plan_validations(
                        normalized_structural, normalized_contextual
                    )
                    attempt["normalization"] = {
                        "changes": list(normalized_changes),
                        "validation": normalized_validation.to_dict(),
                    }
                    parsed = normalized
                    validation = normalized_validation
                    previous = json.dumps(normalized, ensure_ascii=False, indent=2)
                    normalized_path = root / "plans" / base.with_suffix(
                        f".a{index + 1}.normalized.json"
                    )
                    normalized_path.write_text(previous + "\n", encoding="utf-8")
            if parsed is not None:
                last_parsed = parsed
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
            if inter_stage_delay > 0 and index + 1 < plan_attempts:
                time.sleep(inter_stage_delay)
        plan_retry_count = max(0, sum(item["phase"] == "plan" for item in attempts) - 1)
        if plan_text is None:
            fallback, fallback_changes = synthesize_minimal_plan(case, last_parsed)
            if fallback_changes:
                fallback_structural = parse_plan(
                    json.dumps(fallback, ensure_ascii=False), allow_legacy_aliases=False
                )[1]
                fallback_contextual = validate_plan_context(
                    fallback,
                    case.get("constraint_terms", []),
                    required_gates_allowed=bool(
                        case.get("required_gate") or case.get("required_enforcement_patterns")
                    ),
                    soft_preference_only=bool(case.get("soft_preference")),
                )
                fallback_validation = merge_plan_validations(fallback_structural, fallback_contextual)
                if fallback_validation.valid:
                    fallback_text = json.dumps(fallback, ensure_ascii=False, indent=2)
                    fallback_path = root / "plans" / base.with_suffix(".fallback.json")
                    fallback_path.write_text(fallback_text + "\n", encoding="utf-8")
                    attempts.append({
                        "phase": "plan_fallback",
                        "attempt": 1,
                        "success": True,
                        "normalization": {"changes": list(fallback_changes)},
                        "validation": fallback_validation.to_dict(),
                    })
                    state.add_attempt(
                        Stage.PLAN,
                        "fallback",
                        errors=[],
                        evidence={"changes": list(fallback_changes), "validation": fallback_validation.to_dict()},
                    )
                    plan_text = fallback_text
                    plan_fallback_used = True
            if plan_text is None:
                state.finish(False, "plan_validation_exhausted")
                return {
                    "case_id": case["id"], "mode": mode, "model": model, "executor": executor_name, "repeat": repeat,
                    "success": False, "contract_pass": False,
                    "termination_reason": "plan_validation_exhausted", "attempts": attempts,
                    "plan_retry_count": plan_retry_count, "artifact_retry_count": 0,
                    "retry_count": plan_retry_count,
                    "transport_retry_count": sum(int(item.get("transport_retry_count", 0) or 0) for item in attempts),
                    "plan_fallback_used": False,
                    "usage": usage, "state": state.to_dict(), "signature": job_signature,
                }

    if plan_text is not None and inter_stage_delay > 0:
        time.sleep(inter_stage_delay)

    output_path = root / "messages" / base.with_suffix(".json")
    trace_path = root / "traces" / "execute" / base.with_suffix(".jsonl")
    before_execute = project_fingerprints(workspace)
    call = dispatch_generation(
        runtime_prompt(case, mode, plan_text), model, effort, workspace, root / ".codex-homes",
        output_path, trace_path, timeout, ARTIFACT_BUNDLE_SCHEMA_PATH, sandbox="read-only",
        executor_name=executor_name, transport_attempts=transport_attempts,
    )
    add_usage(usage, call["usage"])
    applied_paths, bundle_errors = (
        apply_artifact_bundle(output_path, workspace, case["allowed_paths"])
        if call["success"]
        else ([], [])
    )
    execute_success = call["success"] and not bundle_errors
    execute_changed = changed_project_paths(before_execute, project_fingerprints(workspace))
    attempts.append({
        "phase": "execute",
        "changed_paths": execute_changed,
        "applied_paths": applied_paths,
        "bundle_errors": bundle_errors,
        **call,
    })
    state.add_attempt(
        Stage.EXECUTE,
        "pass" if execute_success else "fail",
        errors=bundle_errors or ([] if call["success"] else ["EXECUTION_CALL_FAILED"]),
        changed_paths=execute_changed,
        evidence={
            "call": call,
            "artifact_bundle": {"applied_paths": applied_paths, "errors": bundle_errors},
        },
    )
    validations = validate_runtime(case, workspace, execute_changed) if execute_success else []
    state.update_validations(validations)
    errors = bundle_errors or [
        error for result in validations if result["status"] == "fail" for error in result["errors"]
    ]
    unsupported = [result for result in validations if result["status"] == "unsupported"]
    if execute_success:
        validation_status = "fail" if errors else ("unsupported" if unsupported else "pass")
        state.add_attempt(Stage.VALIDATE, validation_status, errors=errors, evidence={"validations": validations})
    repair_count = 0
    repair_success = False
    if mode == "full-v2":
        for index in range(repair_attempts):
            if not errors:
                break
            if inter_stage_delay > 0:
                time.sleep(inter_stage_delay)
            repair_count += 1
            repair_output = root / "messages" / "repairs" / base.with_suffix(f".a{index + 1}.json")
            repair_trace = root / "traces" / "repairs" / base.with_suffix(f".a{index + 1}.jsonl")
            before_repair = project_fingerprints(workspace)
            repair = dispatch_generation(
                runtime_prompt(case, mode, plan_text, errors), model, effort, workspace, root / ".codex-homes",
                repair_output, repair_trace, timeout, ARTIFACT_BUNDLE_SCHEMA_PATH, sandbox="read-only",
                executor_name=executor_name, transport_attempts=transport_attempts,
            )
            add_usage(usage, repair["usage"])
            repair_applied_paths, repair_bundle_errors = (
                apply_artifact_bundle(repair_output, workspace, case["allowed_paths"])
                if repair["success"]
                else ([], [])
            )
            repair_applied = repair["success"] and not repair_bundle_errors
            repair_changed = changed_project_paths(before_repair, project_fingerprints(workspace))
            attempts.append({
                "phase": "repair",
                "attempt": index + 1,
                "changed_paths": repair_changed,
                "applied_paths": repair_applied_paths,
                "bundle_errors": repair_bundle_errors,
                **repair,
            })
            state.add_attempt(
                Stage.REPAIR,
                "pass" if repair_applied else "fail",
                errors=repair_bundle_errors or ([] if repair["success"] else ["REPAIR_CALL_FAILED"]),
                changed_paths=repair_changed,
                evidence={
                    "call": repair,
                    "artifact_bundle": {
                        "applied_paths": repair_applied_paths,
                        "errors": repair_bundle_errors,
                    },
                },
            )
            if repair_applied:
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
            elif repair_bundle_errors:
                errors = repair_bundle_errors
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
    artifact_applied = execute_success or repair_success
    contract_pass = artifact_applied and not errors and not unsupported
    termination_reason = (
        "success" if contract_pass else
        "validation_unsupported" if unsupported else
        "artifact_bundle_exhausted" if errors and all(error.startswith("ARTIFACT_") for error in errors) else
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
        "plan_fallback_used": plan_fallback_used,
        "state": state.to_dict(), "signature": job_signature,
    }


def checkpoint(path: Path, metadata: dict, rows: dict[tuple[str, int, str, str], dict]) -> None:
    payload = {**metadata, "results": list(rows.values())}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if (
        args.plan_attempts < 1
        or args.repair_attempts < 0
        or args.transport_attempts < 1
        or args.inter_stage_delay < 0
    ):
        raise ValueError("retry attempt counts must be non-negative and plan attempts must be positive")
    modes = list(dict.fromkeys(args.modes or MODES))
    unknown = set(modes) - set(MODES)
    if unknown:
        raise ValueError(f"Unknown modes: {', '.join(sorted(unknown))}")
    cases = load_cases(args.case_ids, args.cases)
    root = args.output_root / args.experiment
    results_path = root / "runtime-results.json"
    rows: dict[tuple[str, int, str, str], dict] = {}
    if args.resume and results_path.is_file():
        previous = json.loads(results_path.read_text(encoding="utf-8"))
        if (
            previous.get("executor", "codex") == args.executor
            and previous.get("protocol") == RUNTIME_PROTOCOL
        ):
            rows = {(row["model"], row["repeat"], row["mode"], row["case_id"]): row for row in previous["results"]}
    jobs = []
    for model in args.models:
        for repeat in range(1, args.repeat + 1):
            for mode in modes:
                for case in cases:
                    key = (model, repeat, mode, case["id"])
                    expected_signature = runtime_signature(
                        case,
                        mode,
                        model,
                        repeat,
                        args.reasoning_effort,
                        args.executor,
                        args.transport_attempts,
                        args.inter_stage_delay,
                    )
                    if (
                        args.resume
                        and rows.get(key, {}).get("contract_pass")
                        and rows[key].get("signature") == expected_signature
                    ):
                        continue
                    jobs.append((case, mode, model, repeat))
    metadata = {
        "experiment": args.experiment, "protocol": RUNTIME_PROTOCOL,
        "generated_at": datetime.now(timezone.utc).isoformat(), "models": args.models,
        "executor": args.executor, "transport_attempts": args.transport_attempts,
        "execution_runtime_policy": execution_runtime_policy(args.executor),
        "artifact_bundle": artifact_bundle_metadata(),
        "generation_sandbox": "read-only",
        "artifact_application": "validated-runner-atomic-replace",
        "runtime_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inter_stage_delay": args.inter_stage_delay,
        "modes": modes, "repeats": args.repeat, "cases": [case["id"] for case in cases],
        "case_sources": [{
            "path": (
                args.cases.resolve().relative_to(ROOT).as_posix()
                if args.cases.resolve().is_relative_to(ROOT)
                else str(args.cases.resolve())
            ),
            "sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        }],
    }
    checkpoint(results_path, metadata, rows)
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_map = {
            executor.submit(run_runtime_job, case, mode, model, repeat, root, args.reasoning_effort,
                            args.timeout, args.repair_attempts, args.plan_attempts,
                            args.executor, args.transport_attempts, args.inter_stage_delay):
                (case, mode, model, repeat)
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
                             "error": repr(exc),
                             "signature": runtime_signature(
                                 case, mode, model, repeat, args.reasoning_effort, args.executor,
                                 args.transport_attempts, args.inter_stage_delay,
                             )}
            checkpoint(results_path, metadata, rows)
            print(f"[runtime] {model} r{repeat} {mode} {case['id']} pass={rows[key].get('contract_pass')}", flush=True)
    return 0 if all(row.get("contract_pass") for row in rows.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
