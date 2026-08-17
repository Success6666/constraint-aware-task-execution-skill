"""Aggregate matrix, runtime, and validator evidence without hiding missing rows."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any, Mapping

try:
    from .capability_metrics import CapabilityPolicy, aggregate_capability_metrics, evaluate_capability_acceptance
except ImportError:  # Direct script execution.
    from capability_metrics import CapabilityPolicy, aggregate_capability_metrics, evaluate_capability_acceptance


SCORE_METRICS = (
    "evaluation_pass",
    "objective_coverage",
    "constraint_adherence",
    "required_enforcement_coverage",
    "under_enforcement_hits",
    "overoptimization_score",
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evals" / "benchmark-manifest.json"


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


def _release_candidates(
    payload: Mapping[str, Any], manifest: Mapping[str, Any] | None
) -> list[str]:
    explicit = payload.get("release_candidates")
    if isinstance(explicit, list):
        return [str(value) for value in explicit]
    configured = (manifest or {}).get("release_candidates_by_experiment", {})
    if isinstance(configured, Mapping):
        experiment = str(payload.get("experiment", "unknown"))
        selected = configured.get(experiment, configured.get("default", []))
        if isinstance(selected, list):
            return [str(value) for value in selected]
    semantic_review = (manifest or {}).get("semantic_review", {})
    if isinstance(semantic_review, Mapping):
        selected = semantic_review.get("candidate_variants")
        if isinstance(selected, list):
            return [str(value) for value in selected]
    return [
        variant for variant in ("full-v2", "skill", "v1-full")
        if variant in payload.get("variants", [])
    ][:1]


def _capability_acceptance(
    capability: Mapping[str, Any], candidates: list[str], manifest: Mapping[str, Any] | None
) -> dict[str, Any]:
    gates = (manifest or {}).get("release_gates", {})
    if not isinstance(gates, Mapping):
        gates = {}
    semantic_review = (manifest or {}).get("semantic_review", {})
    if not isinstance(semantic_review, Mapping):
        semantic_review = {}
    return evaluate_capability_acceptance(
        capability,
        candidates,
        quality_retention_floor=float(gates.get("quality_retention_ratio_min", 1.0)),
        semantic_retention_floor=float(
            semantic_review.get(
                "minimum_retention_ratio",
                gates.get("semantic_quality_retention_min", 1.0),
            )
        ),
        require_semantic_review=bool(
            semantic_review.get(
                "required_for_final_release",
                gates.get("semantic_review_required", False),
            )
        ),
    )


def summarize_matrix(
    payload: dict[str, Any], manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    payload = _normalize_matrix_payload(payload)
    expected = _expected_keys(payload, "variants")
    raw_rows = payload.get("results", [])
    observed_keys = [
        (row.get("model"), row.get("repeat"), row.get("variant"), row.get("case_id"))
        for row in raw_rows
    ]
    indexed = {
        (row.get("model"), row.get("repeat"), row.get("variant"), row.get("case_id")): row
        for row in raw_rows
    }
    missing = sorted(expected - set(indexed))
    unexpected = sorted(set(indexed) - expected)
    duplicate_rows = len(observed_keys) - len(set(observed_keys))
    failed = [row for row in indexed.values() if not row.get("success")]
    completed = [
        row for row in indexed.values()
        if row.get("success") and row.get("artifact_contract_pass", True)
    ]
    unsupported = [
        row for row in indexed.values()
        if row.get("success")
        and "artifact_contract_pass" in row
        and row.get("artifact_contract_pass") is None
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
    semantic_review = (manifest or {}).get("semantic_review", {})
    minimum_reviewers = (
        int(semantic_review.get("minimum_reviewers_per_pair", 2))
        if isinstance(semantic_review, Mapping)
        else 2
    )
    capability = aggregate_capability_metrics(
        raw_rows,
        policy=CapabilityPolicy(minimum_semantic_reviewers=minimum_reviewers),
    )
    final_candidates = _release_candidates(payload, manifest)
    capability["acceptance"] = _capability_acceptance(
        capability, final_candidates, manifest
    )
    review_required = bool(
        isinstance(semantic_review, Mapping)
        and semantic_review.get("required_for_final_release")
    )
    pairwise_review = payload.get("pairwise_review")
    pairwise_review_complete = (
        not review_required
        or bool(
            isinstance(pairwise_review, Mapping)
            and pairwise_review.get("complete")
            and len(pairwise_review.get("reviewers", [])) >= minimum_reviewers
        )
    )
    if manifest is not None and not pairwise_review_complete:
        capability["acceptance"].setdefault("failures", []).append({
            "variant": ",".join(final_candidates) or "release-candidate",
            "reasons": ["pairwise_review_incomplete"],
        })
        capability["acceptance"]["status"] = "fail"
    capability_accepted = (
        capability["acceptance"]["status"] == "pass"
        and pairwise_review_complete
        if manifest is not None
        else True
    )
    return {
        "experiment": payload.get("experiment", "unknown"),
        "expected": len(expected),
        "observed": len(indexed),
        "completed": len(completed),
        "failed": len(failed),
        "unsupported": len(unsupported),
        "missing": len(missing),
        "complete": (
            len(completed) == len(expected)
            and not failed
            and not unsupported
            and not missing
            and not unexpected
            and not duplicate_rows
        ),
        "missing_keys": [list(key) for key in missing],
        "unexpected_keys": [list(key) for key in unexpected],
        "duplicate_rows": duplicate_rows,
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
        "transport_retry_count": sum(int(row.get("transport_retry_count", 0) or 0) for row in indexed.values()),
        "retry_rows": len(retry_rows),
        "retry_rate": round(len(retry_rows) / observed, 4) if observed else None,
        "repair_success_rate": round(len(repaired) / len(retry_rows), 4) if retry_rows else None,
        "plan_retry_count": sum(int(row.get("plan_retry_count", 0) or 0) for row in indexed.values()),
        "plan_retry_rate": round(len(plan_retry_rows) / observed, 4) if observed else None,
        "artifact_retry_count": sum(int(row.get("artifact_retry_count", 0) or 0) for row in indexed.values()),
        "artifact_retry_rate": round(len(artifact_retry_rows) / observed, 4) if observed else None,
        "usage": usage,
        "capability_retention": capability,
        "capability_accepted": capability_accepted,
        "pairwise_review": pairwise_review,
        "pairwise_review_complete": pairwise_review_complete,
    }


def summarize_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    expected = _expected_keys(payload, "modes")
    raw_rows = payload.get("results", [])
    observed_keys = [
        (row.get("model"), row.get("repeat"), row.get("mode"), row.get("case_id"))
        for row in raw_rows
    ]
    indexed = {
        (row.get("model"), row.get("repeat"), row.get("mode"), row.get("case_id")): row
        for row in raw_rows
    }
    missing = sorted(expected - set(indexed))
    unexpected = sorted(set(indexed) - expected)
    duplicate_rows = len(observed_keys) - len(set(observed_keys))
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
        "complete": (
            len(passed) == len(expected)
            and not failed
            and not missing
            and not unexpected
            and not duplicate_rows
        ),
        "missing_keys": [list(key) for key in missing],
        "unexpected_keys": [list(key) for key in unexpected],
        "duplicate_rows": duplicate_rows,
        "retry_count": sum(int(row.get("retry_count", 0) or 0) for row in indexed.values()),
        "transport_retry_count": sum(int(row.get("transport_retry_count", 0) or 0) for row in indexed.values()),
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
    manifest: Mapping[str, Any] | None = None,
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    matrix_summaries = [summarize_matrix(payload, manifest) for payload in matrices]
    runtime_summaries = [summarize_runtime(payload) for payload in runtimes]
    protocol_summaries = [summarize_protocol(payload) for payload in (protocols or [])]
    validator_complete = bool(
        validator
        and validator.get("accuracy") == 1.0
        and not validator.get("failures")
    )
    preflight_complete = preflight is None or preflight.get("status") == "pass"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if (
            matrix_summaries
            and runtime_summaries
            and protocol_summaries
            and all(item["complete"] for item in matrix_summaries)
            and all(item["capability_accepted"] for item in matrix_summaries)
            and all(item["complete"] for item in runtime_summaries)
            and all(item["complete"] for item in protocol_summaries)
            and validator_complete
            and preflight_complete
        ) else "incomplete",
        "matrices": matrix_summaries,
        "runtimes": runtime_summaries,
        "protocols": protocol_summaries,
        "validator": validator,
        "validator_complete": validator_complete,
        "preflight": dict(preflight) if preflight is not None else None,
        "preflight_complete": preflight_complete,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Complete Experiment Report",
        "",
        f"- Status: `{summary['status']}`",
        f"- Generated: `{summary['generated_at']}`",
        f"- Release preflight: `{'pass' if summary.get('preflight_complete') else 'fail'}`",
        "",
        "## Matrix Experiments",
        "",
        "| Experiment | Expected | Completed | Failed | Unsupported | Missing | Capability | Complete |",
        "| --- | ---: | ---: | ---: | ---: | ---: | :---: | :---: |",
    ]
    for item in summary["matrices"]:
        lines.append(
            f"| `{item['experiment']}` | {item['expected']} | {item['completed']} | "
            f"{item['failed']} | {item['unsupported']} | {item['missing']} | "
            f"{'yes' if item['capability_accepted'] else 'no'} | "
            f"{'yes' if item['complete'] else 'no'} |"
        )
    lines.extend(["", "## Variant Metrics", ""])
    for item in summary["matrices"]:
        def show_capability(value: Any) -> str:
            return "n/a" if value is None else f"{value:.4f}"

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
        if item["unexpected_keys"]:
            lines.extend(["", f"Unexpected rows: `{len(item['unexpected_keys'])}`."])
        if item["duplicate_rows"]:
            lines.extend(["", f"Duplicate rows: `{item['duplicate_rows']}`."])
        lines.append("")
        capability = item.get("capability_retention", {})
        lines.extend([
            "Capability retention (paired against `baseline`):",
            "",
            "| Variant | Pairs | Quality Retention | Non-Constraint Retention | Semantic Coverage | Valid Information | Regression Rate | Efficiency Regression | Cost Ratio | Latency Ratio |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for variant, metrics in capability.get("by_variant", {}).items():
            lines.append(
                f"| `{variant}` | {metrics['paired_rows']}/{metrics['eligible_rows']} | "
                f"{show_capability(metrics['quality_retention_ratio'])} | "
                f"{show_capability(metrics['non_constraint_requirement_retention'])} | "
                f"{show_capability(metrics['semantic_review_coverage'])} | "
                f"{show_capability(metrics['valid_information_retention'])} | "
                f"{show_capability(metrics['capability_regression_rate'])} | "
                f"{show_capability(metrics['efficiency_regression_rate'])} | "
                f"{show_capability(metrics['cost_ratio'])} | "
                f"{show_capability(metrics['latency_ratio'])} |"
            )
        lines.extend([
            "",
            f"Pair coverage: `{capability.get('paired_rows', 0)}/{capability.get('eligible_variant_rows', 0)}`. "
            "Missing pairs are not converted to zero scores.",
            f"General semantic capability status: `{capability.get('semantic_capability_status', 'unsupported')}`. "
            "Broad semantic preservation requires explicit evaluator observations; deterministic regex metrics provide only partial evidence.",
            f"Final-candidate capability acceptance: `{capability.get('acceptance', {}).get('status', 'unsupported')}`.",
            f"Semantic review coverage required: `{capability.get('acceptance', {}).get('semantic_review_required', False)}`.",
            "",
        ])
        acceptance_failures = capability.get("acceptance", {}).get("failures", [])
        if acceptance_failures:
            lines.append("Capability gate failures:")
            lines.append("")
            for failure in acceptance_failures:
                reasons = ", ".join(failure.get("reasons", [])) or "unknown"
                lines.append(f"- `{failure.get('variant', 'unknown')}`: `{reasons}`")
            lines.append("")
        pairwise = item.get("pairwise_review") or {}
        if pairwise:
            lines.extend([
                "Pairwise review state:",
                "",
                f"- Reviewers: `{len(pairwise.get('reviewers', []))}`",
                f"- Accepted pairs: `{pairwise.get('accepted_pairs', 0)}/{pairwise.get('available_pairs', 0)}`",
                f"- Status counts: `{json.dumps(pairwise.get('status_counts', {}), sort_keys=True)}`",
                "",
            ])
            unresolved = [
                pair for pair in pairwise.get("pairs", [])
                if pair.get("status") not in {"accepted", "adjudicated"}
            ]
            for pair in unresolved:
                lines.append(
                    f"- `{pair.get('review_id')}` / `{pair.get('case_id')}` / "
                    f"`{pair.get('variant')}`: `{pair.get('status')}` "
                    f"({', '.join(pair.get('disputed_dimensions', [])) or 'no completed dispute signal'})"
                )
            if unresolved:
                lines.append("")
        for variant, metrics in capability.get("by_variant", {}).items():
            lines.extend([
                f"Component and behavior retention for `{variant}`:",
                "",
                "| Signal | Observed Pairs | Missing Candidate Evidence | Retention / Regression Rate |",
                "| --- | ---: | ---: | ---: |",
            ])
            for component, component_metrics in metrics.get("component_retention", {}).items():
                lines.append(
                    f"| `{component}` retention | {component_metrics['observed_pairs']} | "
                    f"{component_metrics['missing_variant_evidence_hits']} | "
                    f"{show_capability(component_metrics['retention_ratio'])} |"
                )
            for behavior, behavior_metrics in metrics.get("behavioral_regressions", {}).items():
                lines.append(
                    f"| `{behavior}` regression | {behavior_metrics['observed_pairs']} | "
                    "0 | "
                    f"{show_capability(behavior_metrics['regression_rate'])} |"
                )
            lines.append("")
    lines.extend([
        "## Runtime Experiments",
        "",
        "| Experiment | Expected | Passed | Failed | Missing | Targeted Retries | Transport Retries | Complete |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ])
    for item in summary["runtimes"]:
        lines.append(
            f"| `{item['experiment']}` | {item['expected']} | {item['passed']} | {item['failed']} | "
            f"{item['missing']} | {item['retry_count']} | {item['transport_retry_count']} | "
            f"{'yes' if item['complete'] else 'no'} |"
        )
        if item["unexpected_keys"]:
            lines.append(
                f"Unexpected runtime rows for `{item['experiment']}`: `{len(item['unexpected_keys'])}`."
            )
        if item["duplicate_rows"]:
            lines.append(
                f"Duplicate runtime rows for `{item['experiment']}`: `{item['duplicate_rows']}`."
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
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    summary = build_summary(
        [_load(path) for path in args.matrix],
        [_load(path) for path in args.runtime],
        _load(args.validator) if args.validator else None,
        [_load(path) for path in args.protocol],
        _load(args.manifest) if args.manifest else None,
        _load(args.preflight) if args.preflight else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(summary), encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
