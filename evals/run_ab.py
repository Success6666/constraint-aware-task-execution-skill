from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scorer import score_response


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "cases.json"
SKILL_PATH = ROOT / "skills" / "constraint-exec"
RESULTS_PATH = ROOT / "evals" / "results"
WORKSPACES_PATH = ROOT / "evals" / "workspaces"
CODEX_HOMES_PATH = WORKSPACES_PATH / ".codex-homes"
RUNNER_PROTOCOL = "isolated-codex-home-answer-only-v8-developer-instructions-usage"
PROVIDER_REQUEST_RETRIES = 0
PROVIDER_STREAM_RETRIES = 0
ABLATION_VARIANTS = (
    "baseline",
    "skill",
    "positive-framing",
    "structured-plan",
    "plan-validation",
    "full-v2",
)
EVALUATION_RULES = """# Evaluation Workspace

Answer the user's design request directly in the final response. Do not inspect, create, edit, or
patch files and do not run commands. Do not discuss the workspace, permissions, tools, or evaluation.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run baseline/Skill and V2 ablation evaluations.")
    parser.add_argument("--variant", choices=(*ABLATION_VARIANTS, "both", "ablation"), default="both")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--model", help="Optional Codex model override")
    parser.add_argument("--base-url", default=None, help="Optional OpenAI-compatible Codex base URL")
    parser.add_argument("--transport", choices=("codex", "responses"), default="codex")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"))
    parser.add_argument("--verbosity", choices=("low", "medium", "high"))
    parser.add_argument("--output-root", type=Path, default=RESULTS_PATH)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rerun selected cases while preserving other resumed results")
    parser.add_argument("--jobs", type=int, default=1, help="Maximum number of isolated cases to run concurrently")
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
    (workspace / "AGENTS.md").write_text(EVALUATION_RULES, encoding="utf-8")
    return workspace


def skill_instructions() -> str:
    text = (SKILL_PATH / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---\n"):
        _, _, text = text.split("---\n", 2)
    return text.strip()


def prepare_codex_home(case_id: str, variant: str) -> Path:
    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    source_auth = source_home / "auth.json"
    if not source_auth.is_file():
        raise RuntimeError(f"Codex authentication was not found at {source_auth}")

    CODEX_HOMES_PATH.mkdir(parents=True, exist_ok=True)
    codex_home = Path(tempfile.mkdtemp(prefix=f"{variant}-{case_id}-", dir=CODEX_HOMES_PATH))
    shutil.copy2(source_auth, codex_home / "auth.json")
    return codex_home


def load_api_key() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        raise RuntimeError(f"Codex authentication was not found at {auth_path}")
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    api_key = auth.get("OPENAI_API_KEY")
    if not isinstance(api_key, str) or not api_key:
        raise RuntimeError(f"OPENAI_API_KEY was not found in {auth_path}")
    return api_key


def build_prompt(case: dict, variant: str) -> str:
    task = case["prompt"]
    variant_instruction = {
        "positive-framing": (
            " Keep the primary objective explicit, describe constraints as boundaries, and avoid repeating the "
            "forbidden option."
        ),
        "structured-plan": (
            " Return a JSON execution plan with objective, hard_constraints, soft_preferences, risk_points, "
            "artifacts, and validation_profile before giving any implementation detail."
        ),
        "plan-validation": (
            " Return a JSON execution plan and include only deterministic validation checks that the user explicitly "
            "requires or that are needed for safety, format, path, or test contracts."
        ),
        "full-v2": (
            " Return a JSON execution plan, separate constraints from implementation strategies and failure gates, "
            "and describe targeted repair levels without returning scores or self-congratulation."
        ),
    }.get(variant, "")
    task += variant_instruction
    return task + (
        "\n\nThis is an answer-only design task. Do not inspect, create, edit, or patch files, and do not "
        "run tools or commands. Return a concrete, implementation-ready answer focused on what should "
        "be built, key design decisions, and verification. The final answer must contain the complete "
        "requested design and must not discuss files, workspace permissions, tools, or this evaluation."
    )


def skill_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(SKILL_PATH.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(SKILL_PATH).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def parse_trace_usage(trace: str) -> dict[str, int] | None:
    usage: dict[str, Any] | None = None
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    if usage is None:
        return None
    normalized = {
        key: int(usage.get(key, 0) or 0)
        for key in (
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
        )
    }
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return normalized


def parse_response_payload(payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    texts = [
        content.get("text", "")
        for item in payload.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        for content in item.get("content", [])
        if isinstance(content, dict) and content.get("type") == "output_text"
    ]
    response = "\n".join(text for text in texts if text).strip()
    usage = payload.get("usage", {})
    input_details = usage.get("input_tokens_details", {})
    output_details = usage.get("output_tokens_details", {})
    normalized = {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "reasoning_output_tokens": int(output_details.get("reasoning_tokens", 0) or 0),
    }
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    if not response or not normalized["total_tokens"]:
        raise ValueError("Responses API payload did not contain output text and usage evidence")
    return response, normalized


def evaluation_signature(
    case: dict,
    variant: str,
    model: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    verbosity: str | None,
    transport: str,
) -> str:
    payload = {
        "prompt": build_prompt(case, variant),
        "model": model or "codex-default",
        "provider": base_url or "config-default",
        "reasoning_effort": reasoning_effort or "config-default",
        "verbosity": verbosity or "config-default",
        "transport": transport,
        "provider_request_retries": PROVIDER_REQUEST_RETRIES,
        "provider_stream_retries": PROVIDER_STREAM_RETRIES,
        "skill_digest": skill_digest() if variant != "baseline" else None,
        "runner_protocol": RUNNER_PROTOCOL,
        "sandbox": "read-only",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_responses_case(
    case: dict,
    variant: str,
    model: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    timeout: int,
    signature: str,
    verbosity: str | None,
    results_path: Path,
) -> dict:
    if not model or not base_url:
        raise ValueError("Responses transport requires --model and --base-url")
    output_dir = results_path / "raw" / variant
    trace_dir = results_path / "traces" / variant
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{case['id']}.md"
    trace_path = trace_dir / f"{case['id']}.json"
    output_path.unlink(missing_ok=True)
    trace_path.unlink(missing_ok=True)

    request_payload: dict[str, Any] = {
        "model": model,
        "input": build_prompt(case, variant),
    }
    if variant != "baseline":
        request_payload["instructions"] = skill_instructions()
    if reasoning_effort:
        request_payload["reasoning"] = {"effort": reasoning_effort}
    if verbosity:
        request_payload["text"] = {"verbosity": verbosity}
    request = Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {load_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as http_response:
            response_payload = json.loads(http_response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[-2000:]
        return {
            "case_id": case["id"], "variant": variant, "success": False,
            "returncode": error.code, "signature": signature, "error": detail,
        }
    except URLError as error:
        return {
            "case_id": case["id"], "variant": variant, "success": False,
            "returncode": None, "signature": signature, "error": str(error.reason),
        }
    elapsed_seconds = time.perf_counter() - started
    trace_path.write_text(
        json.dumps(response_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    response, usage = parse_response_payload(response_payload)
    output_path.write_text(response + "\n", encoding="utf-8")
    return {
        "case_id": case["id"], "variant": variant, "success": True,
        "returncode": 0, "signature": signature, "usage": usage,
        "timing": {"elapsed_seconds": round(elapsed_seconds, 6)},
        "score": score_response(case, response).to_dict(),
    }


def run_case(
    case: dict,
    variant: str,
    model: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    timeout: int,
    signature: str,
    verbosity: str | None = None,
    results_path: Path = RESULTS_PATH,
) -> dict:
    workspace = prepare_workspace(case["id"], variant)
    codex_home = prepare_codex_home(case["id"], variant)
    output_dir = results_path / "raw" / variant
    trace_dir = results_path / "traces" / variant
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{case['id']}.md"
    trace_path = trace_dir / f"{case['id']}.jsonl"
    output_path.unlink(missing_ok=True)
    trace_path.unlink(missing_ok=True)

    codex_command = shutil.which("codex.cmd") if os.name == "nt" else shutil.which("codex")
    if not codex_command:
        raise RuntimeError("Codex CLI was not found on PATH")
    command = [
        codex_command, "exec", "--ignore-user-config",
        "--disable", "plugins", "--disable", "remote_plugin",
    ]
    if base_url:
        command.extend([
            "--config", 'model_provider="OpenAI"',
            "--config", 'model_providers.OpenAI.name="OpenAI"',
            "--config", 'model_providers.OpenAI.wire_api="responses"',
            "--config", "model_providers.OpenAI.requires_openai_auth=true",
            "--config", f'model_providers.OpenAI.base_url="{base_url}"',
            "--config", f"model_providers.OpenAI.request_max_retries={PROVIDER_REQUEST_RETRIES}",
            "--config", f"model_providers.OpenAI.stream_max_retries={PROVIDER_STREAM_RETRIES}",
        ])
    if variant != "baseline":
        command.extend([
            "--config", f"developer_instructions={json.dumps(skill_instructions())}",
        ])
    command.extend([
        "--ephemeral",
        "--skip-git-repo-check", "--sandbox", "read-only", "--json",
        "--output-last-message", str(output_path), "--cd", str(workspace),
    ])
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    if verbosity:
        command.extend(["--config", f'model_verbosity="{verbosity}"'])
    command.append(build_prompt(case, variant))

    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False, shell=os.name == "nt",
            env=environment,
        )
    finally:
        elapsed_seconds = time.perf_counter() - started
        (codex_home / "auth.json").unlink(missing_ok=True)
        shutil.rmtree(codex_home, ignore_errors=True)
    trace_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0 or not output_path.exists():
        return {
            "case_id": case["id"], "variant": variant, "success": False,
            "returncode": completed.returncode, "signature": signature,
            "error": completed.stderr[-2000:],
        }

    usage = parse_trace_usage(completed.stdout)
    if usage is None:
        return {
            "case_id": case["id"], "variant": variant, "success": False,
            "returncode": completed.returncode, "signature": signature,
            "error": "Successful Codex response did not include turn.completed usage evidence.",
        }

    response = output_path.read_text(encoding="utf-8")
    return {
        "case_id": case["id"], "variant": variant, "success": True,
        "returncode": completed.returncode, "signature": signature,
        "usage": usage,
        "timing": {"elapsed_seconds": round(elapsed_seconds, 6)},
        "score": score_response(case, response).to_dict(),
    }


def write_checkpoint(
    scores_path: Path,
    results: dict[tuple[str, str], dict],
    order: dict[tuple[str, str], int],
    model: str | None,
    reasoning_effort: str | None,
    verbosity: str | None,
    transport: str,
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model or "codex-default",
        "reasoning_effort": reasoning_effort or "config-default",
        "verbosity": verbosity or "config-default",
        "transport": transport,
        "results": [row for _, row in sorted(results.items(), key=lambda item: order.get(item[0], 10**9))],
    }
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = scores_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(scores_path)


def main() -> int:
    args = parse_args()
    if args.jobs < 1 or args.jobs > 8:
        raise SystemExit("--jobs must be between 1 and 8")
    if args.variant == "both":
        variants = ("baseline", "skill")
    elif args.variant == "ablation":
        variants = ABLATION_VARIANTS
    else:
        variants = (args.variant,)
    cases = load_cases(args.case_ids)
    existing: dict[tuple[str, str], dict] = {}
    results_path = args.output_root.resolve()
    scores_path = results_path / "scores.json"
    if args.resume and scores_path.exists():
        previous = json.loads(scores_path.read_text(encoding="utf-8"))
        existing = {
            (row["case_id"], row["variant"]): row
            for row in previous.get("results", [])
        }

    jobs = [(case, variant) for case in cases for variant in variants]
    selected_keys = {(case["id"], variant) for case, variant in jobs}
    result_map = {key: row for key, row in existing.items() if key not in selected_keys}
    order = {key: index for index, key in enumerate(
        [(case["id"], variant) for case in load_cases(None) for variant in ABLATION_VARIANTS]
    )}
    pending: list[tuple[dict, str, str]] = []
    for case, variant in jobs:
        key = (case["id"], variant)
        signature = evaluation_signature(
            case, variant, args.model, args.base_url, args.reasoning_effort, args.verbosity,
            args.transport,
        )
        previous = existing.get(key)
        if (
            previous and previous.get("success") and previous.get("usage")
            and previous.get("timing") and not args.force
            and previous.get("signature") == signature
        ):
            response_path = results_path / "raw" / variant / f"{case['id']}.md"
            if response_path.is_file():
                response = response_path.read_text(encoding="utf-8")
                previous = dict(previous, score=score_response(case, response).to_dict())
                print(f"[rescore] {variant} {case['id']}", flush=True)
                result_map[key] = previous
                continue
        pending.append((case, variant, signature))

    write_checkpoint(
        scores_path, result_map, order, args.model, args.reasoning_effort, args.verbosity,
        args.transport,
    )

    def execute(job: tuple[dict, str, str]) -> dict:
        case, variant, signature = job
        print(f"[{variant}] {case['id']}", flush=True)
        result: dict | None = None
        for attempt in range(1, args.attempts + 1):
            try:
                if args.transport == "responses":
                    result = run_responses_case(
                        case, variant, args.model, args.base_url, args.reasoning_effort,
                        args.timeout, signature, args.verbosity, results_path,
                    )
                else:
                    result = run_case(
                        case, variant, args.model, args.base_url, args.reasoning_effort,
                        args.timeout, signature, args.verbosity, results_path,
                    )
            except Exception as error:
                result = {
                    "case_id": case["id"], "variant": variant, "success": False,
                    "returncode": None, "signature": signature,
                    "error": f"{type(error).__name__}: {error}",
                }
            if result["success"]:
                break
            if attempt < args.attempts:
                print(f"  retry {attempt + 1}/{args.attempts}", flush=True)
                time.sleep(args.retry_delay)
        assert result is not None
        if not result["success"]:
            print(f"  failed: {result['error']}", file=sys.stderr)
        return result

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(execute, job): job for job in pending}
        for future in as_completed(futures):
            case, variant, signature = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {
                    "case_id": case["id"], "variant": variant, "success": False,
                    "returncode": None, "signature": signature,
                    "error": f"{type(error).__name__}: {error}",
                }
                print(f"  failed: {result['error']}", file=sys.stderr)
            key = (result["case_id"], result["variant"])
            result_map[key] = result
            write_checkpoint(
                scores_path, result_map, order, args.model, args.reasoning_effort,
                args.verbosity, args.transport,
            )

    selected_results = [result_map[key] for key in selected_keys]
    return 0 if all(result["success"] for result in selected_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
