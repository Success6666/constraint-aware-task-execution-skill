from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from scorer import score_response


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "cases.json"
SKILL_PATH = ROOT / "constraint-aware-task-execution"
RESULTS_PATH = ROOT / "evals" / "results"
WORKSPACES_PATH = ROOT / "evals" / "workspaces"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run baseline/Skill Codex A/B evaluations.")
    parser.add_argument("--variant", choices=("baseline", "skill", "both"), default="both")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--model", help="Optional Codex model override")
    parser.add_argument("--base-url", default=None, help="Optional OpenAI-compatible Codex base URL")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def load_cases(case_ids: list[str] | None) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not case_ids:
        return cases
    selected = [case for case in cases if case["id"] in set(case_ids)]
    missing = set(case_ids) - {case["id"] for case in selected}
    if missing:
        raise SystemExit(f"Unknown case ids: {', '.join(sorted(missing))}")
    return selected


def prepare_workspace(case_id: str, variant: str) -> Path:
    workspace = WORKSPACES_PATH / variant / case_id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
    if variant == "skill":
        target = workspace / ".codex" / "skills" / SKILL_PATH.name
        target.parent.mkdir(parents=True)
        shutil.copytree(SKILL_PATH, target)
    return workspace


def build_prompt(case: dict, variant: str) -> str:
    task = case["prompt"]
    if variant == "skill":
        task = (
            "Use $constraint-aware-task-execution at "
            ".codex/skills/constraint-aware-task-execution to solve this user request: "
            + task
        )
    return task + (
        "\n\nReturn a concrete, implementation-ready answer. Focus on what should be built, "
        "the key design decisions, and verification. Do not discuss this evaluation."
    )


def run_case(
    case: dict,
    variant: str,
    model: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    timeout: int,
) -> dict:
    workspace = prepare_workspace(case["id"], variant)
    output_dir = RESULTS_PATH / "raw" / variant
    trace_dir = RESULTS_PATH / "traces" / variant
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{case['id']}.md"
    trace_path = trace_dir / f"{case['id']}.jsonl"

    codex_command = shutil.which("codex.cmd") if os.name == "nt" else shutil.which("codex")
    if not codex_command:
        raise RuntimeError("Codex CLI was not found on PATH")
    command = [codex_command, "exec"]
    if base_url:
        command.extend([
            "--ignore-user-config",
            "--config", 'model_provider="OpenAI"',
            "--config", 'model_providers.OpenAI.name="OpenAI"',
            "--config", 'model_providers.OpenAI.wire_api="responses"',
            "--config", "model_providers.OpenAI.requires_openai_auth=true",
            "--config", f'model_providers.OpenAI.base_url="{base_url}"',
        ])
    command.extend([
        "--ignore-rules", "--ephemeral",
        "--skip-git-repo-check", "--sandbox", "read-only", "--json",
        "--output-last-message", str(output_path), "--cd", str(workspace),
    ])
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    command.append(build_prompt(case, variant))

    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False, shell=os.name == "nt",
    )
    trace_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0 or not output_path.exists():
        return {
            "case_id": case["id"], "variant": variant, "success": False,
            "returncode": completed.returncode, "error": completed.stderr[-2000:],
        }

    response = output_path.read_text(encoding="utf-8")
    return {
        "case_id": case["id"], "variant": variant, "success": True,
        "returncode": completed.returncode, "score": score_response(case, response).to_dict(),
    }


def main() -> int:
    args = parse_args()
    variants = ("baseline", "skill") if args.variant == "both" else (args.variant,)
    cases = load_cases(args.case_ids)
    existing: dict[tuple[str, str], dict] = {}
    scores_path = RESULTS_PATH / "scores.json"
    if args.resume and scores_path.exists():
        previous = json.loads(scores_path.read_text(encoding="utf-8"))
        existing = {
            (row["case_id"], row["variant"]): row
            for row in previous.get("results", [])
        }

    run_results = []
    for case in cases:
        for variant in variants:
            previous = existing.get((case["id"], variant))
            if previous and previous.get("success"):
                print(f"[skip] {variant} {case['id']}", flush=True)
                run_results.append(previous)
                continue
            print(f"[{variant}] {case['id']}", flush=True)
            result = None
            for attempt in range(1, args.attempts + 1):
                result = run_case(
                    case, variant, args.model, args.base_url, args.reasoning_effort, args.timeout,
                )
                if result["success"]:
                    break
                if attempt < args.attempts:
                    print(f"  retry {attempt + 1}/{args.attempts}", flush=True)
                    time.sleep(args.retry_delay)
            assert result is not None
            run_results.append(result)
            if not result["success"]:
                print(f"  failed: {result['error']}", file=sys.stderr)

    RESULTS_PATH.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model or "codex-default",
        "reasoning_effort": args.reasoning_effort or "config-default",
        "results": run_results,
    }
    scores_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return 0 if all(result["success"] for result in run_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
