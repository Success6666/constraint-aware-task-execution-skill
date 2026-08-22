from __future__ import annotations

import argparse
import json
from pathlib import Path

from scorer import score_response


ROOT = Path(__file__).resolve().parents[1]
EVALS_PATH = ROOT / "evals"
RESULTS_PATH = EVALS_PATH / "results"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rescore saved A/B responses.")
    parser.add_argument("--output-root", type=Path, default=RESULTS_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results_path = args.output_root.resolve()
    cases = {
        case["id"]: case
        for case in json.loads((EVALS_PATH / "cases.json").read_text(encoding="utf-8"))
    }
    scores_path = results_path / "scores.json"
    payload = json.loads(scores_path.read_text(encoding="utf-8"))

    rescored = 0
    for row in payload["results"]:
        if not row.get("success"):
            continue
        case = cases.get(row["case_id"])
        if case is None:
            raise KeyError(f"Unknown case id: {row['case_id']}")
        response_path = results_path / "raw" / row["variant"] / f"{row['case_id']}.md"
        if not response_path.is_file():
            raise FileNotFoundError(response_path)
        response = response_path.read_text(encoding="utf-8")
        row["score"] = score_response(case, response).to_dict()
        rescored += 1

    scores_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Rescored {rescored} successful responses.")


if __name__ == "__main__":
    main()
