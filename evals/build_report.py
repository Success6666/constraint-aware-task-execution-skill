from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import mean

try:
    from .capability_metrics import CapabilityPolicy, aggregate_capability_metrics, evaluate_capability_acceptance
except ImportError:  # Direct script execution.
    from capability_metrics import CapabilityPolicy, aggregate_capability_metrics, evaluate_capability_acceptance


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "evals" / "results"
CASES_PATH = ROOT / "evals" / "cases.json"
MANIFEST_PATH = ROOT / "evals" / "benchmark-manifest.json"


def average(rows: list[dict], key: str) -> float:
    return round(mean(row["score"][key] for row in rows), 4) if rows else 0.0


def reduction(before: float, after: float) -> float | None:
    return round((before - after) / before * 100, 1) if before else None


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    gates = manifest.get("release_gates", {})
    semantic_review = manifest.get("semantic_review", {})
    language_counts = Counter(case["language"] for case in cases)
    category_counts = Counter(case["category"] for case in cases)
    payload = json.loads((RESULTS_PATH / "scores.json").read_text(encoding="utf-8"))
    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in payload["results"]:
        by_case[row["case_id"]][row["variant"]] = row

    paired_case_ids = [
        case_id for case_id, variants in by_case.items()
        if variants.get("baseline", {}).get("success") and variants.get("skill", {}).get("success")
    ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for case_id in paired_case_ids:
        grouped["baseline"].append(by_case[case_id]["baseline"])
        grouped["skill"].append(by_case[case_id]["skill"])

    metrics = (
        "evaluation_pass", "required_pass", "objective_coverage", "constraint_adherence", "constraint_violation_hits",
        "required_enforcement_coverage", "under_enforcement_hits", "failure_gate_hits", "constraint_component_hits",
        "constraint_echo", "soft_preference_hardening",
    )
    summary = {
        variant: {metric: average(rows, metric) for metric in metrics}
        for variant, rows in grouped.items()
    }
    baseline = summary.get("baseline", {})
    skill = summary.get("skill", {})
    qualified_case_ids = [
        case_id for case_id in paired_case_ids
        if by_case[case_id]["baseline"]["score"]["evaluation_pass"]
        and by_case[case_id]["skill"]["score"]["evaluation_pass"]
    ]
    baseline_overoptimization = average(
        [by_case[case_id]["baseline"] for case_id in qualified_case_ids], "overoptimization_score",
    )
    skill_overoptimization = average(
        [by_case[case_id]["skill"] for case_id in qualified_case_ids], "overoptimization_score",
    )
    baseline["overoptimization_score"] = baseline_overoptimization
    skill["overoptimization_score"] = skill_overoptimization
    summary["comparison"] = {
        "completed_pairs": len(paired_case_ids),
        "qualified_pairs": len(qualified_case_ids),
    }
    summary["suite"] = {
        "cases": len(cases),
        "languages": dict(sorted(language_counts.items())),
        "categories": dict(sorted(category_counts.items())),
    }
    capability = aggregate_capability_metrics(
        payload["results"],
        policy=CapabilityPolicy(
            minimum_semantic_reviewers=int(
                semantic_review.get("minimum_reviewers_per_pair", 2)
            )
        ),
    )
    candidates = manifest.get("release_candidates_by_experiment", {}).get(
        "published-ab", ["skill"]
    )
    semantic_candidates = set(semantic_review.get("candidate_variants", []))
    capability["acceptance"] = evaluate_capability_acceptance(
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
        ) and any(candidate in semantic_candidates for candidate in candidates),
    )
    summary["capability_retention"] = capability
    (RESULTS_PATH / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    improved = worsened = tied = 0
    for case_id in paired_case_ids:
        before = by_case[case_id]["baseline"]["score"]
        after = by_case[case_id]["skill"]["score"]
        if after["evaluation_pass"] and not before["evaluation_pass"]:
            improved += 1
        elif before["evaluation_pass"] and not after["evaluation_pass"]:
            worsened += 1
        elif before["evaluation_pass"] and after["evaluation_pass"]:
            delta = after["overoptimization_score"] - before["overoptimization_score"]
            improved += delta < 0
            worsened += delta > 0
            tied += delta == 0
        else:
            tied += 1
    overoptimization_reduction = reduction(
        baseline_overoptimization, skill_overoptimization,
    )

    lines = [
        "# A/B Evaluation Report", "", f"- Model: `{payload['model']}`",
        f"- Reasoning effort: `{payload.get('reasoning_effort', 'default')}`",
        f"- Cases: `{len(cases)}` (`{language_counts['en']}` English, `{language_counts['zh']}` Chinese)",
        f"- Categories: `{category_counts['hard_constraint']}` hard constraints, "
        f"`{category_counts['soft_preference']}` soft preferences, "
        f"`{category_counts['safety_or_explicit_enforcement']}` safety/explicit enforcement, "
        f"`{category_counts['output_or_architecture_constraint']}` output/architecture constraints", "",
        f"Completed pairs: `{len(paired_case_ids)}/{len(by_case)}`", "",
        "## Aggregate Results", "", "| Metric | Baseline | Skill | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for metric in metrics:
        before, after = baseline.get(metric, 0.0), skill.get(metric, 0.0)
        lines.append(f"| `{metric}` | {before:.4f} | {after:.4f} | {after - before:+.4f} |")

    lines.extend([
        "", "Higher is better for pass, coverage, and adherence metrics; lower is better for the remaining metrics.", "",
        "## Qualified Over-Optimization", "",
        f"Only the `{len(qualified_case_ids)}` pairs where both responses passed objective and constraint gates are compared.", "",
        "| Metric | Baseline | Skill | Delta |", "| --- | ---: | ---: | ---: |",
        f"| `overoptimization_score` | {baseline_overoptimization:.4f} | {skill_overoptimization:.4f} | "
        f"{skill_overoptimization - baseline_overoptimization:+.4f} |", "",
        "## Per-Case Results", "",
        "| Case | Baseline Pass | Skill Pass | Baseline Score | Skill Score | Delta |",
        "| --- | :---: | :---: | ---: | ---: | ---: |",
    ])
    for case_id in sorted(paired_case_ids):
        before_row, after_row = by_case[case_id].get("baseline"), by_case[case_id].get("skill")
        if not before_row or not after_row:
            continue
        before = before_row["score"]["overoptimization_score"]
        after = after_row["score"]["overoptimization_score"]
        before_pass = "yes" if before_row["score"]["evaluation_pass"] else "no"
        after_pass = "yes" if after_row["score"]["evaluation_pass"] else "no"
        lines.append(
            f"| `{case_id}` | {before_pass} | {after_pass} | {before:.2f} | {after:.2f} | {after - before:+.2f} |"
        )

    lines.extend(["", "## Interpretation", ""])
    coverage_before = baseline.get("objective_coverage", 0.0)
    coverage_after = skill.get("objective_coverage", 0.0)
    adherence_before = baseline.get("constraint_adherence", 0.0)
    adherence_after = skill.get("constraint_adherence", 0.0)
    pass_rate_not_lower = skill.get("evaluation_pass", 0.0) >= baseline.get("evaluation_pass", 0.0)
    adherence_not_lower = adherence_after >= adherence_before
    capability_accepted = capability.get("acceptance", {}).get("status") == "pass"
    if (
        overoptimization_reduction is not None
        and pass_rate_not_lower
        and adherence_not_lower
        and capability_accepted
    ):
        coverage_note = (
            "without reducing aggregate objective coverage"
            if coverage_after >= coverage_before
            else f"while aggregate objective coverage changed by `{coverage_after - coverage_before:+.4f}`"
        )
        lines.append(
            f"The aggregate over-optimization score decreased by `{overoptimization_reduction:.1f}%` "
            f"{coverage_note}."
        )
    else:
        lines.append(
            "No overall improvement claim is made because the qualified baseline score was zero, "
            "task/constraint pass rates decreased, or the capability-retention gate did not pass."
        )
    lines.extend([
        "",
        f"Aggregate constraint adherence changed by `{adherence_after - adherence_before:+.4f}`.",
        "",
        f"Per case: `{improved}` improved, `{worsened}` worsened, and `{tied}` tied.",
        "",
        "This is a small, single-model regression experiment with a strong baseline. "
        "The deterministic scorer is not a statistical significance test or a substitute for blind human review. "
        "Inspect the committed raw responses when a metric changes unexpectedly.",
        "",
    ])
    capability_variant = capability.get("by_variant", {}).get("skill", {})
    def show_capability(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.4f}"
    lines.extend([
        "## Capability Retention",
        "",
        f"- Paired rows: `{capability.get('paired_rows', 0)}/{capability.get('eligible_variant_rows', 0)}`",
        f"- Quality retention ratio: `{show_capability(capability_variant.get('quality_retention_ratio'))}`",
        f"- Non-constraint requirement retention: `{show_capability(capability_variant.get('non_constraint_requirement_retention'))}`",
        f"- Capability regression rate: `{show_capability(capability_variant.get('capability_regression_rate'))}`",
        f"- Efficiency regression rate: `{show_capability(capability_variant.get('efficiency_regression_rate'))}`",
        f"- Valid information retention: `{show_capability(capability_variant.get('valid_information_retention'))}`",
        f"- Semantic review coverage: `{show_capability(capability_variant.get('semantic_review_coverage'))}`",
        f"- Cost ratio: `{show_capability(capability_variant.get('cost_ratio'))}`",
        f"- Latency ratio: `{show_capability(capability_variant.get('latency_ratio'))}`",
        f"- General semantic capability: `{capability.get('semantic_capability_status', 'unsupported')}`",
        f"- Capability acceptance: `{capability.get('acceptance', {}).get('status', 'unsupported')}`",
        f"- Semantic review required: `{capability.get('acceptance', {}).get('semantic_review_required', False)}`",
        "",
        "Only successful baseline/skill pairs enter retention denominators. Missing pairs are reported as coverage gaps, not zero scores. General semantic preservation remains partial unless an explicit semantic evaluator supplies observations.",
        "",
    ])
    lines.extend([
        "| Component | Observed Pairs | Missing Candidate Evidence | Retention | Regression Rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for component, metrics in capability_variant.get("component_retention", {}).items():
        lines.append(
            f"| `{component}` | {metrics['observed_pairs']} | "
            f"{metrics['missing_variant_evidence_hits']} | "
            f"{show_capability(metrics['retention_ratio'])} | "
            f"{show_capability(metrics['regression_rate'])} |"
        )
    lines.extend(["", "Behavioral regression observations:", ""])
    for behavior, metrics in capability_variant.get("behavioral_regressions", {}).items():
        lines.append(
            f"- `{behavior}`: `{metrics['regression_hits']}/{metrics['observed_pairs']}` "
            f"(rate `{show_capability(metrics['regression_rate'])}`)"
        )
    failures = capability.get("acceptance", {}).get("failures", [])
    if failures:
        lines.extend(["", "Capability gate failures:", ""])
        for failure in failures:
            reasons = ", ".join(failure.get("reasons", [])) or "unknown"
            lines.append(f"- `{failure.get('variant', 'unknown')}`: `{reasons}`")
    lines.append("")
    (RESULTS_PATH / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
