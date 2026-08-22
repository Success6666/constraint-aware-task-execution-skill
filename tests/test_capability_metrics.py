from __future__ import annotations

import unittest

from evals.capability_metrics import (
    CapabilityPolicy,
    aggregate_capability_metrics,
    evaluate_capability_acceptance,
    pair_results,
)


def row(variant: str, *, success: bool, objective: float, compliance: float) -> dict:
    return {
        "executor": "test",
        "model": "model",
        "case_id": "case",
        "repeat": 1,
        "sampling_signature": "same",
        "variant": variant,
        "success": success,
        "score": {
            "objective_coverage": objective,
            "constraint_compliance": compliance,
            "response_format_compliance": 1.0,
            "path_scope_compliance": 1.0,
        },
    }


class CapabilityMetricTests(unittest.TestCase):
    def test_failed_contract_row_with_score_remains_pairable(self) -> None:
        baseline = row("baseline", success=False, objective=1.0, compliance=0.0)
        candidate = row("full-v2", success=True, objective=1.0, compliance=1.0)

        pairs = pair_results([baseline, candidate])
        summary = aggregate_capability_metrics([baseline, candidate])

        self.assertEqual(pairs, [(baseline, candidate)])
        self.assertEqual(summary["pair_coverage"], 1.0)
        self.assertEqual(summary["capability_regression_hits"], 0)

    def test_transport_failure_without_score_is_not_fabricated(self) -> None:
        baseline = row("baseline", success=True, objective=1.0, compliance=1.0)
        candidate = {
            "executor": "test",
            "model": "model",
            "case_id": "case",
            "repeat": 1,
            "sampling_signature": "same",
            "variant": "full-v2",
            "success": False,
        }

        self.assertEqual(pair_results([baseline, candidate]), [])

    def test_token_growth_fails_release_acceptance(self) -> None:
        baseline = row("baseline", success=True, objective=1.0, compliance=1.0)
        candidate = row("full-v2", success=True, objective=1.0, compliance=1.0)
        baseline.update({"usage": {"input_tokens": 60, "output_tokens": 40}, "elapsed_seconds": 1.0})
        candidate.update({"usage": {"input_tokens": 70, "output_tokens": 40}, "elapsed_seconds": 1.0})

        summary = aggregate_capability_metrics(
            [baseline, candidate],
            policy=CapabilityPolicy(cost_ratio_ceiling=1.0),
        )
        acceptance = evaluate_capability_acceptance(
            summary,
            ["full-v2"],
            cost_ratio_ceiling=1.0,
        )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn("token_cost_ratio", acceptance["failures"][0]["reasons"])

    def test_cost_ratio_uses_total_tokens_not_mean_of_case_ratios(self) -> None:
        rows = []
        for case_id, baseline_cost, candidate_cost in (
            ("large", 1000, 900),
            ("small", 10, 20),
        ):
            baseline = row("baseline", success=True, objective=1.0, compliance=1.0)
            candidate = row("full-v2", success=True, objective=1.0, compliance=1.0)
            baseline["case_id"] = candidate["case_id"] = case_id
            baseline["usage"] = {"total_tokens": baseline_cost}
            candidate["usage"] = {"total_tokens": candidate_cost}
            rows.extend((baseline, candidate))

        summary = aggregate_capability_metrics(rows)

        self.assertAlmostEqual(
            summary["by_variant"]["full-v2"]["cost_ratio"], 920 / 1010
        )

    def test_missing_efficiency_evidence_does_not_pass(self) -> None:
        baseline = row("baseline", success=True, objective=1.0, compliance=1.0)
        candidate = row("full-v2", success=True, objective=1.0, compliance=1.0)

        summary = aggregate_capability_metrics([baseline, candidate])
        acceptance = evaluate_capability_acceptance(summary, ["full-v2"])

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "missing_efficiency_evidence", acceptance["failures"][0]["reasons"]
        )

    def test_stage_latencies_are_summed_for_ratio(self) -> None:
        baseline = row("baseline", success=True, objective=1.0, compliance=1.0)
        candidate = row("full-v2", success=True, objective=1.0, compliance=1.0)
        baseline.update({
            "usage": {"input_tokens": 60, "output_tokens": 40},
            "stages": [{"elapsed_seconds": 0.4}, {"elapsed_seconds": 0.6}],
        })
        candidate.update({
            "usage": {"input_tokens": 60, "output_tokens": 40},
            "stages": [{"elapsed_seconds": 0.5}, {"elapsed_seconds": 1.0}],
        })

        summary = aggregate_capability_metrics([baseline, candidate])

        self.assertEqual(summary["by_variant"]["full-v2"]["latency_ratio"], 1.5)


if __name__ == "__main__":
    unittest.main()
