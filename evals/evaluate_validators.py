"""Evaluate deterministic gate classification against the labeled multilingual set."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

from scorer import structural_gate_hits


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "gate_cases.json"


def evaluate(cases: list[dict]) -> dict[str, Any]:
    confusion = Counter()
    by_language: dict[str, Counter] = defaultdict(Counter)
    failures = []
    for case in cases:
        predicted = structural_gate_hits(case["text"], case["constraint_terms"]) > 0
        expected = bool(case["expect_gate"])
        key = "tp" if predicted and expected else "tn" if not predicted and not expected else "fp" if predicted else "fn"
        confusion[key] += 1
        by_language[case["language"]][key] += 1
        if predicted != expected:
            failures.append({"id": case["id"], "expected": expected, "predicted": predicted})
    tp, fp, fn = confusion["tp"], confusion["fp"], confusion["fn"]
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "cases": len(cases),
        "confusion": dict(confusion),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "accuracy": round((confusion["tp"] + confusion["tn"]) / len(cases), 4) if cases else 0.0,
        "by_language": {language: dict(counts) for language, counts in sorted(by_language.items())},
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate deterministic Gate classification.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.cases.read_text(encoding="utf-8")))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if not result["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
