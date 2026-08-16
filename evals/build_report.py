from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "evals" / "results"


def average(rows: list[dict], key: str) -> float:
    return round(mean(row["score"][key] for row in rows), 4) if rows else 0.0


def reduction(before: float, after: float) -> float | None:
    return round((before - after) / before * 100, 1) if before else None


def main() -> None:
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
        "objective_coverage", "failure_gate_hits", "constraint_component_hits",
        "constraint_echo", "soft_preference_hardening", "overoptimization_score",
    )
    summary = {
        variant: {metric: average(rows, metric) for metric in metrics}
        for variant, rows in grouped.items()
    }
    (RESULTS_PATH / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    baseline = summary.get("baseline", {})
    skill = summary.get("skill", {})
    pair_deltas = [
        by_case[case_id]["skill"]["score"]["overoptimization_score"]
        - by_case[case_id]["baseline"]["score"]["overoptimization_score"]
        for case_id in paired_case_ids
    ]
    improved = sum(delta < 0 for delta in pair_deltas)
    worsened = sum(delta > 0 for delta in pair_deltas)
    tied = sum(delta == 0 for delta in pair_deltas)
    overoptimization_reduction = reduction(
        baseline.get("overoptimization_score", 0.0),
        skill.get("overoptimization_score", 0.0),
    )

    lines = [
        "# A/B Evaluation Report", "", f"- Model: `{payload['model']}`",
        f"- Reasoning effort: `{payload.get('reasoning_effort', 'default')}`", "",
        f"Completed pairs: `{len(paired_case_ids)}/{len(by_case)}`", "",
        "## Aggregate Results", "", "| Metric | Baseline | Skill | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    for metric in metrics:
        before, after = baseline.get(metric, 0.0), skill.get(metric, 0.0)
        lines.append(f"| `{metric}` | {before:.4f} | {after:.4f} | {after - before:+.4f} |")

    lines.extend([
        "", "Lower is better for every metric except `objective_coverage`.", "",
        "## Per-Case Over-Optimization Score", "",
        "| Case | Baseline | Skill | Delta |", "| --- | ---: | ---: | ---: |",
    ])
    for case_id in sorted(paired_case_ids):
        before_row, after_row = by_case[case_id].get("baseline"), by_case[case_id].get("skill")
        if not before_row or not after_row:
            continue
        before = before_row["score"]["overoptimization_score"]
        after = after_row["score"]["overoptimization_score"]
        lines.append(f"| `{case_id}` | {before:.2f} | {after:.2f} | {after - before:+.2f} |")

    lines.extend(["", "## Interpretation", ""])
    coverage_before = baseline.get("objective_coverage", 0.0)
    coverage_after = skill.get("objective_coverage", 0.0)
    if overoptimization_reduction is not None:
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
        lines.append("The baseline aggregate over-optimization score was zero, so no reduction rate is reported.")
    lines.extend([
        "",
        f"Per case: `{improved}` improved, `{worsened}` worsened, and `{tied}` tied.",
        "",
        "This is a small, single-model regression experiment with a strong baseline. "
        "The deterministic scorer is not a statistical significance test or a substitute for blind human review. "
        "Inspect the committed raw responses when a metric changes unexpectedly.",
        "",
    ])
    (RESULTS_PATH / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
