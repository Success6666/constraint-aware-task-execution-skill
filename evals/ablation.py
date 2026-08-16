"""Deterministic helpers for comparing prompt/execution variants.

The module intentionally consumes already-scored rows. It never sends a score
back to a model and never treats a missing artifact as a passing result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


VARIANTS = (
    "baseline",
    "skill",
    "positive-framing",
    "structured-plan",
    "plan-validation",
    "full-v2",
)


@dataclass(frozen=True)
class AblationSummary:
    variant: str
    completed: int
    evaluation_pass: float
    objective_coverage: float
    constraint_adherence: float
    overoptimization_score: float
    under_enforcement_hits: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _average(rows: list[dict], key: str) -> float:
    return round(mean(row["score"][key] for row in rows), 4) if rows else 0.0


def summarize(rows: Iterable[dict]) -> list[AblationSummary]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("success") and row.get("variant") in VARIANTS and row.get("score"):
            grouped[row["variant"]].append(row)
    summaries = []
    for variant in VARIANTS:
        selected = grouped.get(variant, [])
        summaries.append(AblationSummary(
            variant=variant,
            completed=len(selected),
            evaluation_pass=_average(selected, "evaluation_pass"),
            objective_coverage=_average(selected, "objective_coverage"),
            constraint_adherence=_average(selected, "constraint_adherence"),
            overoptimization_score=_average(selected, "overoptimization_score"),
            under_enforcement_hits=_average(selected, "under_enforcement_hits"),
        ))
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize completed V1/V2 ablation rows.")
    parser.add_argument("--scores", type=Path, default=Path(__file__).parent / "results" / "scores.json")
    args = parser.parse_args()
    payload = json.loads(args.scores.read_text(encoding="utf-8"))
    print(json.dumps([summary.to_dict() for summary in summarize(payload.get("results", []))], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
