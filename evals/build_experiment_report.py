"""Aggregate matrix, runtime, and validator evidence without hiding missing rows."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any


SCORE_METRICS = (
    "evaluation_pass",
    "objective_coverage",
    "constraint_adherence",
    "required_enforcement_coverage",
    "under_enforcement_hits",
    "overoptimization_score",
)


def _average(rows: list[dict[str, Any]], metric: str) -> float | None:
    values = [row["score"][metric] for row in rows if metric in row.get("score", {})]
    return round(mean(values), 4) if values else None


def _expected_keys(payload: dict[str, Any], variant_key: str) -> set[tuple[str, int, str, str]]:
    return {
        (model, repeat, variant, case_id)
        for model in payload.get("models", [])
        for repeat in range(1, int(payload.get("repeats", 1)) + 1)
        for variant in payload.get(variant_key, [])
        for case_id in payload.get("cases", [])
    }


def _normalize_matrix_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("models"):
        return payload
    rows = payload.get("results", [])
    model = payload.get("model", "unknown")
    normalized = dict(payload)
    normalized.setdefault("experiment", "published-ab")
    normalized["models"] = [model]
    normalized["variants"] = sorted({row.get("variant") for row in rows if row.get("variant")})
    normalized["cases"] = sorted({row.get("case_id") for row in rows if row.get("case_id")})
    normalized["repeats"] = 1
    normalized["results"] = [
        {"model": model, "repeat": 1, **row}
        for row in rows
    ]
    return normalized


def summarize_matrix(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _normalize_matrix_payload(payload)
    expected = _expected_keys(payload, "variants")
    indexed = {
        (row.get("model"), row.get("repeat"), row.get("variant"), row.get("case_id")): row
        for row in payload.get("results", [])
    }
    missing = sorted(expected - set(indexed))
    failed = [row for row in indexed.values() if not row.get("success")]
    completed = [
        row for row in indexed.values()
        if row.get("success") and row.get("artifact_contract_pass", True)
    ]
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in completed:
        if row.get("score"):
            by_variant[row["variant"]].append(row)
    variant_metrics = {
        variant: {
            "completed": len(by_variant.get(variant, [])),
            **{metric: _average(by_variant.get(variant, []), metric) for metric in SCORE_METRICS},
        }
        for variant in payload.get("variants", [])
    }
    usage = {
        key: sum(int(row.get("usage", {}).get(key, 0) or 0) for row in indexed.values())
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    }
    retry_rows = [row for row in indexed.values() if int(row.get("retry_count", 0) or 0) > 0]
    repaired = [row for row in retry_rows if row.get("repair_success")]
    plan_retry_rows = [row for row in indexed.values() if int(row.get("plan_retry_count", 0) or 0) > 0]
    artifact_retry_rows = [row for row in indexed.values() if int(row.get("artifact_retry_count", 0) or 0) > 0]
    observed = len(indexed)
    return {
        "experiment": payload.get("experiment", "unknown"),
        "expected": len(expected),
        "observed": len(indexed),
        "completed": len(completed),
        "failed": len(failed),
        "missing": len(missing),
        "complete": len(completed) == len(expected) and not failed and not missing,
        "missing_keys": [list(key) for key in missing],
        "failures": [
            {
                "model": row.get("model"),
                "variant": row.get("variant"),
                "case_id": row.get("case_id"),
                "failure_stage": row.get("failure_stage"),
                "termination_reason": row.get("termination_reason"),
            }
            for row in failed
        ],
        "variant_metrics": variant_metrics,
        "retry_count": sum(int(row.get("retry_count", 0) or 0) for row in indexed.values()),
        "retry_rows": len(retry_rows),
        "retry_rate": round(len(retry_rows) / observed, 4) if observed else None,
        "repair_success_rate": round(len(repaired) / len(retry_rows), 4) if retry_rows else None,
        "plan_retry_count": sum(int(row.get("plan_retry_count", 0) or 0) for row in indexed.values()),
        "plan_retry_rate": round(len(plan_retry_rows) / observed, 4) if observed else None,
        "artifact_retry_count": sum(int(row.get("artifact_retry_count", 0) or 0) for row in indexed.values()),
        "artifact_retry_rate": round(len(artifact_retry_rows) / observed, 4) if observed else None,
        "usage": usage,
    }


def summarize_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    expected = _expected_keys(payload, "modes")
    indexed = {
        (row.get("model"), row.get("repeat"), row.get("mode"), row.get("case_id")): row
        for row in payload.get("results", [])
    }
    missing = sorted(expected - set(indexed))
    passed = [row for row in indexed.values() if row.get("success") and row.get("contract_pass")]
    failed = [row for row in indexed.values() if not (row.get("success") and row.get("contract_pass"))]
    retry_rows = [row for row in indexed.values() if int(row.get("retry_count", 0) or 0) > 0]
    observed = len(indexed)
    return {
        "experiment": payload.get("experiment", "unknown"),
        "expected": len(expected),
        "observed": len(indexed),
        "passed": len(passed),
        "failed": len(failed),
        "missing": len(missing),
        "complete": len(passed) == len(expected) and not failed and not missing,
        "missing_keys": [list(key) for key in missing],
        "retry_count": sum(int(row.get("retry_count", 0) or 0) for row in indexed.values()),
        "retry_rate": round(len(retry_rows) / observed, 4) if observed else None,
        "repair_successes": sum(bool(row.get("repair_success")) for row in indexed.values()),
    }


def summarize_protocol(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("results", [])
    expected = int(payload.get("cases", len(rows)))
    passed = sum(bool(row.get("conformance_pass")) for row in rows)
    return {
        "protocol": payload.get("protocol", "unknown"),
        "expected": expected,
        "observed": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "missing": max(0, expected - len(rows)),
        "complete": len(rows) == expected and passed == expected,
        **payload.get("summary", {}),
    }


def build_summary(
    matrices: list[dict[str, Any]],
    runtimes: list[dict[str, Any]],
    validator: dict[str, Any] | None,
    protocols: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    matrix_summaries = [summarize_matrix(payload) for payload in matrices]
    runtime_summaries = [summarize_runtime(payload) for payload in runtimes]
    protocol_summaries = [summarize_protocol(payload) for payload in (protocols or [])]
    validator_complete = bool(
        validator
        and validator.get("accuracy") == 1.0
        and not validator.get("failures")
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if (
            matrix_summaries
            and runtime_summaries
            and protocol_summaries
            and all(item["complete"] for item in matrix_summaries)
            and all(item["complete"] for item in runtime_summaries)
            and all(item["complete"] for item in protocol_summaries)
            and validator_complete
        ) else "incomplete",
        "matrices": matrix_summaries,
        "runtimes": runtime_summaries,
        "protocols": protocol_summaries,
        "validator": validator,
        "validator_complete": validator_complete,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Complete Experiment Report",
        "",
        f"- Status: `{summary['status']}`",
        f"- Generated: `{summary['generated_at']}`",
        "",
        "## Matrix Experiments",
        "",
        "| Experiment | Expected | Completed | Failed | Missing | Complete |",
        "| --- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for item in summary["matrices"]:
        lines.append(
            f"| `{item['experiment']}` | {item['expected']} | {item['completed']} | "
            f"{item['failed']} | {item['missing']} | {'yes' if item['complete'] else 'no'} |"
        )
    lines.extend(["", "## Variant Metrics", ""])
    for item in summary["matrices"]:
        lines.extend([
            f"### {item['experiment']}",
            "",
            "| Variant | N | Eval Pass | Objective | Adherence | Required Enforcement | Under-Enforcement | Over-Optimization |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for variant, metrics in item["variant_metrics"].items():
            def show(value: Any) -> str:
                return "n/a" if value is None else f"{value:.4f}"
            lines.append(
                f"| `{variant}` | {metrics['completed']} | {show(metrics['evaluation_pass'])} | "
                f"{show(metrics['objective_coverage'])} | {show(metrics['constraint_adherence'])} | "
                f"{show(metrics['required_enforcement_coverage'])} | {show(metrics['under_enforcement_hits'])} | "
                f"{show(metrics['overoptimization_score'])} |"
            )
        if item["failures"]:
            lines.extend(["", "Failures:"])
            for failure in item["failures"]:
                lines.append(
                    f"- `{failure['model']}` / `{failure['variant']}` / `{failure['case_id']}`: "
                    f"`{failure['failure_stage'] or failure['termination_reason'] or 'unknown'}`"
                )
        if item["missing_keys"]:
            lines.extend(["", f"Missing rows: `{len(item['missing_keys'])}`."])
        lines.append("")
    lines.extend([
        "## Runtime Experiments",
        "",
        "| Experiment | Expected | Passed | Failed | Missing | Retries | Complete |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: |",
    ])
    for item in summary["runtimes"]:
        lines.append(
            f"| `{item['experiment']}` | {item['expected']} | {item['passed']} | {item['failed']} | "
            f"{item['missing']} | {item['retry_count']} | {'yes' if item['complete'] else 'no'} |"
        )
    lines.extend([
        "",
        "## Protocol Conformance",
        "",
        "| Protocol | Expected | Passed | Failed | Missing | Retry Rate | Average Retries | Complete |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ])
    for item in summary["protocols"]:
        lines.append(
            f"| `{item['protocol']}` | {item['expected']} | {item['passed']} | {item['failed']} | "
            f"{item['missing']} | {item.get('retry_rate', 0):.4f} | {item.get('average_retries', 0):.4f} | "
            f"{'yes' if item['complete'] else 'no'} |"
        )
    validator = summary.get("validator") or {}
    lines.extend([
        "",
        "## Gate Validator",
        "",
        f"- Cases: `{validator.get('cases', 0)}`",
        f"- Accuracy: `{validator.get('accuracy', 'n/a')}`",
        f"- Precision: `{validator.get('precision', 'n/a')}`",
        f"- Recall: `{validator.get('recall', 'n/a')}`",
        f"- Failures: `{len(validator.get('failures', []))}`",
        "",
        "Missing and failed rows are never converted to zero-valued metric samples.",
        "",
    ])
    return "\n".join(lines)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build strict experiment completeness and metric reports.")
    parser.add_argument("--matrix", type=Path, action="append", default=[])
    parser.add_argument("--runtime", type=Path, action="append", default=[])
    parser.add_argument("--protocol", type=Path, action="append", default=[])
    parser.add_argument("--validator", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    summary = build_summary(
        [_load(path) for path in args.matrix],
        [_load(path) for path in args.runtime],
        _load(args.validator) if args.validator else None,
        [_load(path) for path in args.protocol],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(summary), encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
