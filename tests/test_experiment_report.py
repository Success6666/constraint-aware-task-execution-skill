from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from build_experiment_report import build_summary, summarize_matrix  # noqa: E402


def score(value: float) -> dict:
    return {
        "evaluation_pass": 1.0,
        "objective_coverage": 1.0,
        "constraint_adherence": 1.0,
        "required_enforcement_coverage": 1.0,
        "under_enforcement_hits": 0.0,
        "overoptimization_score": value,
    }


class ExperimentReportTests(unittest.TestCase):
    def test_published_ab_payload_is_normalized(self) -> None:
        payload = {
            "model": "model",
            "results": [
                {"variant": "baseline", "case_id": "a", "success": True, "score": score(2.0)},
                {"variant": "skill", "case_id": "a", "success": True, "score": score(1.0)},
            ],
        }
        summary = summarize_matrix(payload)
        self.assertEqual(summary["expected"], 2)
        self.assertEqual(summary["completed"], 2)
        self.assertTrue(summary["complete"])

    def test_missing_and_failed_rows_are_not_metric_samples(self) -> None:
        payload = {
            "experiment": "matrix",
            "models": ["model"],
            "variants": ["baseline", "full-v2"],
            "repeats": 1,
            "cases": ["a", "b"],
            "results": [
                {"model": "model", "repeat": 1, "variant": "baseline", "case_id": "a", "success": True, "score": score(2.0)},
                {"model": "model", "repeat": 1, "variant": "baseline", "case_id": "b", "success": False, "score": score(99.0)},
                {"model": "model", "repeat": 1, "variant": "full-v2", "case_id": "a", "success": True, "score": score(1.0), "retry_count": 1, "artifact_retry_count": 1, "repair_success": True},
            ],
        }
        summary = summarize_matrix(payload)
        self.assertEqual(summary["expected"], 4)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["variant_metrics"]["baseline"]["overoptimization_score"], 2.0)
        self.assertEqual(summary["retry_rate"], 0.3333)
        self.assertEqual(summary["repair_success_rate"], 1.0)
        self.assertFalse(summary["complete"])

    def test_complete_status_requires_matrix_runtime_and_validator(self) -> None:
        matrix = {
            "experiment": "matrix", "models": ["model"], "variants": ["baseline"],
            "repeats": 1, "cases": ["a"],
            "results": [{"model": "model", "repeat": 1, "variant": "baseline", "case_id": "a", "success": True, "score": score(0.0)}],
        }
        runtime = {
            "experiment": "runtime", "models": ["model"], "modes": ["full-v2"],
            "repeats": 1, "cases": ["a"],
            "results": [{"model": "model", "repeat": 1, "mode": "full-v2", "case_id": "a", "success": True, "contract_pass": True}],
        }
        validator = {"cases": 1, "accuracy": 1.0, "precision": 1.0, "recall": 1.0, "failures": []}
        protocol = {
            "protocol": "fixture", "cases": 1,
            "results": [{"case_id": "a", "conformance_pass": True}],
            "summary": {"passed": 1, "failed": 0, "retry_rate": 0.0, "average_retries": 0.0},
        }
        self.assertEqual(build_summary([matrix], [runtime], validator, [protocol])["status"], "complete")
        runtime["results"][0]["contract_pass"] = False
        self.assertEqual(build_summary([matrix], [runtime], validator, [protocol])["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
