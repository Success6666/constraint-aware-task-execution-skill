"""Manifest-driven release verification and experiment orchestration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
from typing import Any, Iterable, Mapping

from experiment_variants import VARIANTS
from redaction import atomic_write_json, atomic_write_text, redact_text


ROOT = Path(__file__).resolve().parents[1]
EVALS_ROOT = ROOT / "evals"
DEFAULT_CONFIG = ROOT / "evals" / "release-experiment.json"
PHASES = (
    "verification",
    "deterministic",
    "matrix",
    "review-prepare",
    "review-apply",
    "runtime",
    "report",
)
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete release evidence plan.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--phase", action="append", choices=PHASES)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--allow-large-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-untagged", action="store_true")
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise ValueError("release experiment config must be a schema_version=1.0 object")
    for key in ("release_candidate", "experiment", "output_root", "limits", "matrices", "runtimes"):
        if key not in value:
            raise ValueError(f"missing release experiment field: {key}")
    if not ID_PATTERN.fullmatch(str(value["experiment"])):
        raise ValueError("experiment must contain only lowercase letters, digits, dot, dash, or underscore")
    ids: set[str] = set()
    for group in ("matrices", "runtimes"):
        if not isinstance(value[group], list):
            raise ValueError(f"{group} must be an array")
        for item in value[group]:
            if not isinstance(item, dict) or not ID_PATTERN.fullmatch(str(item.get("id", ""))):
                raise ValueError(f"invalid {group} id")
            if item["id"] in ids:
                raise ValueError(f"duplicate experiment id: {item['id']}")
            ids.add(item["id"])
            if item.get("executor") not in {"codex", "ollama"}:
                raise ValueError(f"unsupported executor in {item['id']}")
            if not item.get("model"):
                raise ValueError(f"missing model in {item['id']}")
    review = value.get("review", {})
    if review.get("required_for_final_release"):
        reviewers = review.get("reviewers")
        minimum = int(review.get("minimum_reviewers_per_pair", 0))
        if not isinstance(reviewers, list) or len(reviewers) < 2:
            raise ValueError("final release review requires at least two reviewers")
        if len({str(reviewer) for reviewer in reviewers}) != len(reviewers):
            raise ValueError("reviewer ids must be unique")
        if any(not ID_PATTERN.fullmatch(str(reviewer)) for reviewer in reviewers):
            raise ValueError("reviewer ids must be safe lowercase identifiers")
        if minimum < 2 or minimum > len(reviewers):
            raise ValueError("minimum_reviewers_per_pair must be between 2 and reviewer count")
        candidates = review.get("candidate_variants")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("final release review requires candidate_variants")
        for item in value["matrices"]:
            missing_candidates = set(candidates) - set(item.get("variants", []))
            if missing_candidates:
                raise ValueError(
                    f"matrix {item['id']} is missing review candidates: {sorted(missing_candidates)}"
                )
    return value


def config_digest(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def case_count(path: Path) -> int:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"case file must be an array: {path}")
    return len(value)


def case_file_path(value: Any) -> Path:
    path = (EVALS_ROOT / str(value)).resolve()
    if path != EVALS_ROOT and EVALS_ROOT not in path.parents:
        raise ValueError(f"case file escapes evals directory: {value}")
    return path


def estimate_model_jobs(config: Mapping[str, Any]) -> dict[str, int]:
    matrix_jobs = sum(
        sum(case_count(case_file_path(path)) for path in item.get("case_files", []))
        * len(item.get("variants", [])) * int(item.get("repeats", 1))
        for item in config["matrices"]
    )
    runtime_jobs = sum(
        case_count(case_file_path(item.get("case_file")))
        * len(item.get("modes", [])) * int(item.get("repeats", 1))
        for item in config["runtimes"]
    )
    matrix_invocations = 0
    for item in config["matrices"]:
        matrix_cases = sum(
            case_count(case_file_path(path)) for path in item.get("case_files", [])
        )
        per_case = 0
        for name in item.get("variants", []):
            variant = VARIANTS.get(str(name))
            if variant is None:
                raise ValueError(f"unknown variant in release config: {name}")
            plan_calls = int(item.get("plan_attempts", 2)) if variant.structured_plan and variant.validate_plan else int(variant.structured_plan)
            repair_calls = int(item.get("artifact_attempts", 2)) if variant.repair_artifact else 0
            per_case += 1 + plan_calls + repair_calls
        matrix_invocations += matrix_cases * int(item.get("repeats", 1)) * per_case * int(item.get("transport_attempts", 1))
    runtime_invocations = 0
    for item in config["runtimes"]:
        runtime_cases = case_count(case_file_path(item.get("case_file")))
        per_case = 0
        for mode in item.get("modes", []):
            per_case += 1 if mode == "direct" else 1 + int(item.get("plan_attempts", 2)) + int(item.get("repair_attempts", 2))
        runtime_invocations += runtime_cases * int(item.get("repeats", 1)) * per_case * int(item.get("transport_attempts", 1))
    return {
        "matrix": matrix_jobs,
        "runtime": runtime_jobs,
        "total": matrix_jobs + runtime_jobs,
        "max_matrix_invocations": matrix_invocations,
        "max_runtime_invocations": runtime_invocations,
        "max_total_invocations": matrix_invocations + runtime_invocations,
    }


def resolve_output_root(config: Mapping[str, Any], override: Path | None) -> Path:
    configured = override or Path(str(config["output_root"]))
    base = configured if configured.is_absolute() else ROOT / configured
    return (base / str(config["experiment"])).resolve()


def executable_status(executor: str) -> dict[str, Any]:
    command = "codex" if executor == "codex" else "ollama"
    resolved = shutil.which(command)
    return {"executor": executor, "command": command, "available": bool(resolved), "path": resolved}


def git_provenance() -> dict[str, Any]:
    def capture(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, shell=False,
        )
        return completed.stdout.strip()
    return {
        "commit": capture("rev-parse", "HEAD"),
        "branch": capture("branch", "--show-current"),
        "dirty": bool(capture("status", "--porcelain")),
        "tags": capture("tag", "--points-at", "HEAD").splitlines(),
    }


def build_provenance(config: Mapping[str, Any], jobs: Mapping[str, int]) -> dict[str, Any]:
    executors = sorted({str(item["executor"]) for item in [*config["matrices"], *config["runtimes"]]})
    manifest = json.loads((ROOT / "evals" / "benchmark-manifest.json").read_text(encoding="utf-8"))
    datasets = []
    for item in manifest.get("datasets", []):
        path = ROOT / "evals" / str(item["path"])
        datasets.append({
            "id": item["id"],
            "path": str(path.relative_to(ROOT)),
            "declared_sha256": item["sha256"],
            "observed_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_candidate": config["release_candidate"],
        "config_digest": config_digest(config),
        "git": git_provenance(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "executors": [executable_status(name) for name in executors],
        "datasets": datasets,
        "estimated_jobs": dict(jobs),
    }


def matrix_command(item: Mapping[str, Any], output_root: Path, resume: bool) -> list[str]:
    command = [
        sys.executable, str(ROOT / "evals" / "run_matrix.py"),
        "--experiment", str(item["id"]),
        "--output-root", str(output_root),
        "--executor", str(item["executor"]),
        "--model", str(item["model"]),
        "--reasoning-effort", str(item.get("reasoning_effort", "medium")),
        "--repeat", str(item.get("repeats", 1)),
        "--jobs", str(item.get("jobs", 1)),
        "--timeout", str(item.get("timeout_seconds", 600)),
        "--transport-attempts", str(item.get("transport_attempts", 2)),
        "--inter-stage-delay", str(item.get("inter_stage_delay_seconds", 0)),
        "--plan-attempts", str(item.get("plan_attempts", 2)),
        "--artifact-attempts", str(item.get("artifact_attempts", 2)),
    ]
    for case_file in item.get("case_files", []):
        command.extend(("--cases", str(case_file_path(case_file))))
    for variant in item.get("variants", []):
        command.extend(("--variant", str(variant)))
    if resume:
        command.append("--resume")
    return command


def runtime_command(item: Mapping[str, Any], output_root: Path, resume: bool) -> list[str]:
    command = [
        sys.executable, str(ROOT / "evals" / "run_runtime.py"),
        "--experiment", str(item["id"]),
        "--output-root", str(output_root),
        "--executor", str(item["executor"]),
        "--model", str(item["model"]),
        "--reasoning-effort", str(item.get("reasoning_effort", "medium")),
        "--repeat", str(item.get("repeats", 1)),
        "--jobs", str(item.get("jobs", 1)),
        "--timeout", str(item.get("timeout_seconds", 900)),
        "--transport-attempts", str(item.get("transport_attempts", 2)),
        "--inter-stage-delay", str(item.get("inter_stage_delay_seconds", 0)),
        "--cases", str(case_file_path(item.get("case_file"))),
        "--plan-attempts", str(item.get("plan_attempts", 2)),
        "--repair-attempts", str(item.get("repair_attempts", 2)),
    ]
    for mode in item.get("modes", []):
        command.extend(("--mode", str(mode)))
    if resume:
        command.append("--resume")
    return command


def _review_packet_path(release_root: Path, matrix_id: str, reviewer_id: str) -> Path:
    return release_root / "reviews" / f"{matrix_id}.{reviewer_id}.json"


def _review_key_path(release_root: Path, matrix_id: str, reviewer_id: str) -> Path:
    return release_root / "review-keys" / f"{matrix_id}.{reviewer_id}.key.json"


def _review_seed_path(release_root: Path, matrix_id: str, reviewer_id: str) -> Path:
    return release_root / "review-keys" / f"{matrix_id}.{reviewer_id}.seed"


def _reviewed_result_path(output_root: Path, matrix_id: str) -> Path:
    return output_root / matrix_id / "results.reviewed.json"


def review_prepare_command(
    config: Mapping[str, Any],
    item: Mapping[str, Any],
    reviewer_id: str,
    output_root: Path,
    release_root: Path,
    force: bool = False,
) -> list[str]:
    matrix_id = str(item["id"])
    command = [
        sys.executable,
        str(ROOT / "evals" / "pairwise_review.py"),
        "prepare",
        "--results",
        str(output_root / matrix_id / "results.json"),
        "--experiment-root",
        str(output_root / matrix_id),
        "--seed-file",
        str(_review_seed_path(release_root, matrix_id, reviewer_id)),
        "--reviewer-id",
        reviewer_id,
        "--reviews",
        str(_review_packet_path(release_root, matrix_id, reviewer_id)),
        "--key",
        str(_review_key_path(release_root, matrix_id, reviewer_id)),
    ]
    for case_file in item.get("case_files", []):
        command.extend(("--cases", str(case_file_path(case_file))))
    for variant in config.get("review", {}).get("candidate_variants", []):
        command.extend(("--variant", str(variant)))
    if force:
        command.append("--force")
    return command


def review_apply_command(
    config: Mapping[str, Any],
    item: Mapping[str, Any],
    output_root: Path,
    release_root: Path,
) -> list[str]:
    review = config.get("review", {})
    matrix_id = str(item["id"])
    command = [
        sys.executable,
        str(ROOT / "evals" / "pairwise_review.py"),
        "apply",
        "--results",
        str(output_root / matrix_id / "results.json"),
        "--minimum-reviewers",
        str(review.get("minimum_reviewers_per_pair", 2)),
        "--disagreement-threshold",
        str(review.get("maximum_dimension_delta_spread", 0.2)),
        "--output",
        str(_reviewed_result_path(output_root, matrix_id)),
    ]
    for reviewer_id in review.get("reviewers", []):
        command.extend(
            ("--reviews", str(_review_packet_path(release_root, matrix_id, str(reviewer_id))))
        )
        command.extend(
            ("--key", str(_review_key_path(release_root, matrix_id, str(reviewer_id))))
        )
    adjudications = release_root / "reviews" / f"{matrix_id}.adjudications.json"
    if adjudications.is_file():
        command.extend(("--adjudications", str(adjudications)))
    return command


def report_command(config: Mapping[str, Any], output_root: Path, release_root: Path) -> list[str]:
    command = [sys.executable, str(ROOT / "evals" / "build_experiment_report.py")]
    for item in config["matrices"]:
        matrix_id = str(item["id"])
        matrix_result = (
            _reviewed_result_path(output_root, matrix_id)
            if config.get("review", {}).get("required_for_final_release")
            else output_root / matrix_id / "results.json"
        )
        command.extend(("--matrix", str(matrix_result)))
    for item in config["runtimes"]:
        command.extend(("--runtime", str(output_root / str(item["id"]) / "runtime-results.json")))
    command.extend((
        "--preflight", str(release_root / "release-preflight.json"),
        "--protocol", str(release_root / "protocol-fixtures.json"),
        "--protocol", str(release_root / "runtime-fixtures.json"),
        "--validator", str(release_root / "gate-validator.json"),
        "--output", str(release_root / "REPORT.md"),
        "--json-output", str(release_root / "summary.json"),
    ))
    return command


def build_steps(
    config: Mapping[str, Any],
    output_root: Path,
    release_root: Path,
    resume: bool,
    force: bool = False,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if config.get("verification", {}).get("enabled", True):
        steps.extend((
            {"id": "unit-tests", "phase": "verification", "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], "evidence": []},
            {"id": "compile", "phase": "verification", "command": [sys.executable, "-m", "compileall", "-q", "evals", "scripts", "tests"], "evidence": []},
            {"id": "verify-install", "phase": "verification", "command": [sys.executable, str(ROOT / "scripts" / "verify-install.py")], "evidence": []},
        ))
    if config.get("deterministic", {}).get("enabled", True):
        max_attempts = str(config.get("deterministic", {}).get("protocol_max_attempts", 2))
        steps.extend((
            {
                "id": "release-preflight",
                "phase": "deterministic",
                "command": [
                    sys.executable,
                    str(ROOT / "evals" / "verify_release_contract.py"),
                    "--manifest", str(ROOT / "evals" / "benchmark-manifest.json"),
                    "--release", str(ROOT / "evals" / "release-experiment.json"),
                    "--output", str(release_root / "release-preflight.json"),
                ],
                "evidence": [release_root / "release-preflight.json"],
            },
            {"id": "protocol-fixtures", "phase": "deterministic", "command": [sys.executable, str(ROOT / "evals" / "run_protocol_fixtures.py"), "--output", str(release_root / "protocol-fixtures.json"), "--max-attempts", max_attempts], "evidence": [release_root / "protocol-fixtures.json"]},
            {"id": "runtime-fixtures", "phase": "deterministic", "command": [sys.executable, str(ROOT / "evals" / "run_runtime_fixtures.py"), "--output", str(release_root / "runtime-fixtures.json")], "evidence": [release_root / "runtime-fixtures.json"]},
            {"id": "gate-validator", "phase": "deterministic", "command": [sys.executable, str(ROOT / "evals" / "evaluate_validators.py"), "--output", str(release_root / "gate-validator.json")], "evidence": [release_root / "gate-validator.json"]},
        ))
    for item in config["matrices"]:
        steps.append({"id": f"matrix-{item['id']}", "phase": "matrix", "command": matrix_command(item, output_root, resume), "evidence": [output_root / str(item["id"]) / "results.json"]})
    review = config.get("review", {})
    if review.get("required_for_final_release"):
        for item in config["matrices"]:
            matrix_id = str(item["id"])
            for reviewer_id in review.get("reviewers", []):
                reviewer = str(reviewer_id)
                steps.append({
                    "id": f"review-prepare-{matrix_id}-{reviewer}",
                    "phase": "review-prepare",
                    "command": review_prepare_command(
                        config, item, reviewer, output_root, release_root, force
                    ),
                    "evidence": [
                        _review_packet_path(release_root, matrix_id, reviewer),
                        _review_key_path(release_root, matrix_id, reviewer),
                    ],
                })
            steps.append({
                "id": f"review-apply-{matrix_id}",
                "phase": "review-apply",
                "command": review_apply_command(config, item, output_root, release_root),
                "evidence": [_reviewed_result_path(output_root, matrix_id)],
                "continue_on_failure": True,
            })
    for item in config["runtimes"]:
        steps.append({"id": f"runtime-{item['id']}", "phase": "runtime", "command": runtime_command(item, output_root, resume), "evidence": [output_root / str(item["id"]) / "runtime-results.json"]})
    steps.append({"id": "release-report", "phase": "report", "command": report_command(config, output_root, release_root), "evidence": [release_root / "REPORT.md", release_root / "summary.json"]})
    return steps


def ensure_review_seeds(config: Mapping[str, Any], release_root: Path) -> None:
    review = config.get("review", {})
    if not review.get("required_for_final_release"):
        return
    for item in config["matrices"]:
        matrix_id = str(item["id"])
        for reviewer_id in review.get("reviewers", []):
            seed_path = _review_seed_path(release_root, matrix_id, str(reviewer_id))
            if seed_path.exists():
                continue
            seed_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(seed_path, secrets.token_urlsafe(48) + "\n")


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.kill()


def run_step(step: Mapping[str, Any], release_root: Path, timeout: int) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(step["command"]), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", shell=False, creationflags=creationflags,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        stdout, stderr = process.communicate()
    finished = datetime.now(timezone.utc)
    log_path = release_root / "logs" / f"{step['id']}.log"
    atomic_write_text(log_path, f"STDOUT\n{stdout}\nSTDERR\n{stderr}\n")
    success = not timed_out and process.returncode == 0 and all(Path(path).exists() for path in step.get("evidence", []))
    return {
        "id": step["id"],
        "phase": step["phase"],
        "success": success,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "command": [redact_text(str(item)) for item in step["command"]],
        "log": str(log_path.relative_to(release_root)),
        "evidence": [str(Path(path).relative_to(release_root)) if release_root in Path(path).parents else str(path) for path in step.get("evidence", [])],
    }


def selected_steps(steps: Iterable[dict[str, Any]], phases: list[str] | None) -> list[dict[str, Any]]:
    wanted = set(phases or PHASES)
    return [step for step in steps if step["phase"] in wanted]


def main() -> int:
    args = parse_args()
    config = load_config(args.config.resolve())
    jobs = estimate_model_jobs(config)
    max_jobs = int(config.get("limits", {}).get("max_model_jobs", 0))
    if max_jobs and jobs["total"] > max_jobs and not args.allow_large_run:
        raise ValueError(f"estimated model jobs {jobs['total']} exceed configured maximum {max_jobs}")
    max_invocations = int(config.get("limits", {}).get("max_model_invocations", 0))
    if max_invocations and jobs["max_total_invocations"] > max_invocations and not args.allow_large_run:
        raise ValueError(
            f"estimated maximum model invocations {jobs['max_total_invocations']} exceed configured maximum {max_invocations}"
        )
    output_root = resolve_output_root(config, args.output_root)
    release_root = output_root / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    provenance = build_provenance(config, jobs)
    atomic_write_json(release_root / "provenance.json", provenance)
    selected = set(args.phase or PHASES)
    active_executors: set[str] = set()
    if "matrix" in selected:
        active_executors.update(str(item["executor"]) for item in config["matrices"])
    if "runtime" in selected:
        active_executors.update(str(item["executor"]) for item in config["runtimes"])
    unavailable = [item for item in provenance["executors"] if item["executor"] in active_executors and not item["available"]]
    if unavailable and not args.dry_run:
        raise RuntimeError(f"required executors unavailable: {', '.join(item['executor'] for item in unavailable)}")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version != str(config["release_candidate"]):
        raise RuntimeError(f"VERSION={version} does not match release_candidate={config['release_candidate']}")
    if provenance["git"]["dirty"] and not (args.dry_run or args.allow_dirty):
        raise RuntimeError("release evidence requires a clean worktree; use --allow-dirty only for diagnostics")
    expected_tag = f"v{config['release_candidate']}"
    if expected_tag not in provenance["git"]["tags"] and not (args.dry_run or args.allow_untagged):
        raise RuntimeError(f"HEAD must be tagged {expected_tag} before release evidence is collected")
    if not args.dry_run and selected & {"review-prepare", "review-apply"}:
        ensure_review_seeds(config, release_root)
    steps = selected_steps(
        build_steps(config, output_root, release_root, args.resume, args.force),
        args.phase,
    )
    plan = {
        "schema_version": "1.0",
        "release_candidate": config["release_candidate"],
        "config_digest": config_digest(config),
        "estimated_jobs": jobs,
        "steps": [{"id": step["id"], "phase": step["phase"], "command": step["command"], "evidence": [str(path) for path in step["evidence"]]} for step in steps],
    }
    atomic_write_json(release_root / "plan.json", plan)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    state_path = release_root / "state.json"
    state: dict[str, Any] = {"config_digest": config_digest(config), "steps": {}}
    if args.resume and state_path.is_file():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if previous.get("config_digest") != state["config_digest"] and not args.force:
            raise RuntimeError("release config changed; use --force or a new experiment id")
        state = previous
    timeout = int(config.get("limits", {}).get("step_timeout_seconds", 172800))
    failed = False
    for step in steps:
        previous = state["steps"].get(step["id"])
        evidence_exists = all(Path(path).exists() for path in step.get("evidence", []))
        if args.resume and previous and previous.get("success") and evidence_exists:
            continue
        result = run_step(step, release_root, timeout)
        state["steps"][step["id"]] = result
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(state_path, state)
        print(f"[{step['phase']}] {step['id']} success={result['success']}", flush=True)
        if not result["success"]:
            failed = True
            if not args.continue_on_error and not step.get("continue_on_failure"):
                break
    state["status"] = "failed" if failed else "complete"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(state_path, state)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
