from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from scorer import score_response  # noqa: E402


class ScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = {
            "constraint_terms": ["redis"],
            "objective_markers": ["fastapi", "redirect", "test"],
            "soft_preference": False,
        }

    def test_good_answer_preserves_objective_without_gate(self) -> None:
        score = score_response(self.case, "Build the FastAPI redirect endpoint, persist aliases in SQLite, and add tests.")
        self.assertEqual(score.objective_coverage, 1.0)
        self.assertEqual(score.failure_gate_hits, 0)
        self.assertEqual(score.constraint_component_hits, 0)
        self.assertEqual(score.constraint_echo, 0)

    def test_detector_and_repeated_constraint_are_penalized(self) -> None:
        score = score_response(self.case, "Add RedisDetector and RedisGuard. Fail CI if Redis appears. Redis must never be used.")
        self.assertGreater(score.failure_gate_hits, 0)
        self.assertGreater(score.constraint_component_hits, 0)
        self.assertGreater(score.constraint_echo, 0)
        self.assertGreater(score.overoptimization_score, 0)

    def test_soft_preference_hardening_is_penalized(self) -> None:
        score = score_response(dict(self.case, soft_preference=True), "Strictly prohibit Redis and always reject it.")
        self.assertGreater(score.soft_preference_hardening, 0)

    def test_objective_markers_accept_semantic_alternatives(self) -> None:
        case = dict(self.case, objective_markers=["配置|config|application.yml|datasource"])
        score = score_response(case, "Set the datasource in application.yml.")
        self.assertEqual(score.objective_coverage, 1.0)

    def test_failure_gate_scoring_can_be_disabled_for_gate_objectives(self) -> None:
        case = dict(self.case, score_failure_gates=False)
        score = score_response(case, "Reject a request after the Redis-backed rate limit is exceeded.")
        self.assertEqual(score.failure_gate_hits, 0)

    def test_case_set_has_required_coverage(self) -> None:
        cases = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 10)
        self.assertTrue(any(case["soft_preference"] for case in cases))
        self.assertTrue(any(case["language"] == "zh" for case in cases))
        self.assertTrue(any(case["language"] == "en" for case in cases))


if __name__ == "__main__":
    unittest.main()
