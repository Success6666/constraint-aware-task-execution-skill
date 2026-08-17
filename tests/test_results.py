from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from scorer import score_response  # noqa: E402


class ResultArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = {
            case["id"]: case
            for case in json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
        }
        cls.scores = json.loads(
            (ROOT / "evals" / "results" / "scores.json").read_text(encoding="utf-8")
        )

    def test_scores_cover_every_case_and_variant(self) -> None:
        expected = {
            (case_id, variant)
            for case_id in self.cases
            for variant in ("baseline", "skill")
        }
        actual = {(row["case_id"], row["variant"]) for row in self.scores["results"]}
        self.assertEqual(actual, expected)
        self.assertEqual(len(self.scores["results"]), 60)
        self.assertTrue(all(row["success"] for row in self.scores["results"]))

    def test_committed_scores_match_raw_responses(self) -> None:
        for row in self.scores["results"]:
            response_path = (
                ROOT / "evals" / "results" / "raw" / row["variant"] / f"{row['case_id']}.md"
            )
            response = response_path.read_text(encoding="utf-8")
            expected = score_response(self.cases[row["case_id"]], response).to_dict()
            self.assertEqual(row["score"], expected, f"Stale score for {row['variant']}:{row['case_id']}")

    def test_summary_and_report_match_release_claim(self) -> None:
        summary = json.loads(
            (ROOT / "evals" / "results" / "summary.json").read_text(encoding="utf-8")
        )
        report = (ROOT / "evals" / "results" / "REPORT.md").read_text(encoding="utf-8")
        self.assertEqual(summary["comparison"], {"completed_pairs": 30, "qualified_pairs": 30})
        self.assertEqual(summary["baseline"]["evaluation_pass"], 1.0)
        self.assertEqual(summary["skill"]["evaluation_pass"], 1.0)
        self.assertIn("decreased by `80.0%`", report)
        self.assertIn("`7` improved, `0` worsened, and `23` tied", report)


if __name__ == "__main__":
    unittest.main()
