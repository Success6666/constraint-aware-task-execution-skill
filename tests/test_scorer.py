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
            "forbidden_adoption_terms": ["redis"],
            "soft_preference": False,
        }

    def test_good_answer_preserves_objective_without_gate(self) -> None:
        score = score_response(self.case, "Build the FastAPI redirect endpoint, persist aliases in SQLite, and add tests.")
        self.assertEqual(score.objective_coverage, 1.0)
        self.assertEqual(score.constraint_adherence, 1.0)
        self.assertEqual(score.constraint_violation_hits, 0)
        self.assertEqual(score.failure_gate_hits, 0)
        self.assertEqual(score.constraint_component_hits, 0)
        self.assertEqual(score.constraint_echo, 0)

    def test_detector_and_repeated_constraint_are_penalized(self) -> None:
        score = score_response(self.case, "Add RedisDetector and RedisGuard. Fail CI if Redis appears. Redis must never be used.")
        self.assertGreater(score.failure_gate_hits, 0)
        self.assertGreater(score.constraint_component_hits, 0)
        self.assertGreater(score.constraint_echo, 0)
        self.assertGreater(score.overoptimization_score, 0)

    def test_structural_gate_requires_target_mechanism_and_failure_action(self) -> None:
        gated = score_response(
            self.case,
            "Add RedisDetector. Fail CI if Redis appears. Build the FastAPI redirect service and tests.",
        )
        mechanism_only = score_response(
            self.case,
            "Document a RedisDetector for diagnostics. Build the FastAPI redirect service and tests.",
        )
        separated_mechanism_only = score_response(
            self.case,
            "Add a scanner for Redis. Build the FastAPI redirect service and tests.",
        )
        failure_only = score_response(
            self.case,
            "Reject Redis requests when needed. Build the FastAPI redirect service and tests.",
        )
        self.assertEqual(gated.failure_gate_hits, 1)
        self.assertEqual(mechanism_only.failure_gate_hits, 0)
        self.assertEqual(separated_mechanism_only.failure_gate_hits, 0)
        self.assertEqual(failure_only.failure_gate_hits, 0)

    def test_negated_structural_gate_is_not_counted(self) -> None:
        responses = (
            "Do not automatically reject the project if Redis appears. Build FastAPI redirects and tests.",
            "Redis must not be added to a scanner. Build FastAPI redirects and tests.",
            "Redis should not be scanned. Build FastAPI redirects and tests.",
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(score_response(self.case, response).failure_gate_hits, 0)

    def test_actual_forbidden_tool_adoption_is_detected(self) -> None:
        score = score_response(self.case, "Use Redis for persistence, then implement FastAPI redirects and tests.")
        self.assertEqual(score.constraint_adherence, 0.0)
        self.assertGreater(score.constraint_violation_hits, 0)
        self.assertFalse(score.required_pass)
        self.assertFalse(score.evaluation_pass)

    def test_third_person_backed_and_config_adoption_are_detected(self) -> None:
        responses = (
            "The design uses Redis for FastAPI redirects and tests.",
            "Use Redis-backed persistence for FastAPI redirects and tests.",
            "Storage: Redis. Build FastAPI redirects and tests.",
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertGreater(score_response(self.case, response).constraint_violation_hits, 0)

    def test_chinese_adoption_is_detected(self) -> None:
        case = dict(self.case, constraint_terms=["celery"], forbidden_adoption_terms=["celery"])
        score = score_response(case, "由 Celery 承担调度，并实现 FastAPI redirect 和 test。")
        self.assertGreater(score.constraint_violation_hits, 0)

    def test_negated_forbidden_tool_adoption_is_not_a_violation(self) -> None:
        score = score_response(self.case, "Do not use Redis. Implement FastAPI redirects and tests with SQLite.")
        self.assertEqual(score.constraint_adherence, 1.0)
        self.assertEqual(score.constraint_violation_hits, 0)

    def test_negated_dependency_is_not_a_violation(self) -> None:
        score = score_response(self.case, "We do not depend on Redis. Build FastAPI redirects and tests with SQLite.")
        self.assertEqual(score.constraint_violation_hits, 0)

    def test_chinese_negated_adoption_is_not_a_violation(self) -> None:
        score = score_response(self.case, "不引入 Redis，使用 SQLite 实现 FastAPI redirect 和 test。")
        self.assertEqual(score.constraint_violation_hits, 0)

    def test_chinese_modal_negation_is_not_a_violation(self) -> None:
        responses = (
            "这个方案不能使用 Redis，使用 SQLite 实现 FastAPI redirect 和 test。",
            "系统不可采用 Redis，使用 SQLite 实现 FastAPI redirect 和 test。",
            "服务不会依赖 Redis，使用 SQLite 实现 FastAPI redirect 和 test。",
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(score_response(self.case, response).constraint_violation_hits, 0)

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

    def test_required_enforcement_is_measured_without_penalizing_it(self) -> None:
        case = dict(
            self.case,
            objective_markers=["magic|signature", "reject|quarantine", "test"],
            forbidden_adoption_terms=[],
            required_enforcement_patterns=["magic|signature", "reject|quarantine", "test"],
            score_failure_gates=False,
            score_constraint_components=False,
        )
        response = "Validate the file magic signature, reject executable uploads, and add tests."
        score = score_response(case, response)
        self.assertEqual(score.required_enforcement_coverage, 1.0)
        self.assertEqual(score.under_enforcement_hits, 0)
        self.assertEqual(score.constraint_adherence, 1.0)
        self.assertEqual(score.failure_gate_hits, 0)
        self.assertEqual(score.constraint_component_hits, 0)
        self.assertEqual(score.constraint_echo, 0)
        self.assertEqual(score.overoptimization_score, 0.0)

    def test_negated_enforcement_does_not_satisfy_requirement(self) -> None:
        case = dict(
            self.case,
            forbidden_adoption_terms=[],
            required_enforcement_patterns=["reject|block"],
            score_failure_gates=False,
            score_constraint_components=False,
        )
        score = score_response(case, "Do not reject invalid signatures. Build FastAPI redirects and tests.")
        self.assertEqual(score.required_enforcement_coverage, 0.0)
        self.assertEqual(score.under_enforcement_hits, 1)
        self.assertFalse(score.required_pass)

    def test_under_enforcement_counts_each_missing_required_group(self) -> None:
        case = dict(
            self.case,
            forbidden_adoption_terms=[],
            required_enforcement_patterns=["dns", "redirect", "block|reject", "test"],
            score_failure_gates=False,
            score_constraint_components=False,
        )
        score = score_response(case, "Fetch the URL and add tests.")
        self.assertEqual(score.under_enforcement_hits, 3)
        self.assertAlmostEqual(score.required_enforcement_coverage, 0.25)

    def test_json_and_file_scope_constraints_are_hard_gates(self) -> None:
        json_case = dict(self.case, forbidden_adoption_terms=[], required_response_format="json_object")
        self.assertTrue(score_response(json_case, '{"fastapi":"redirect test"}').required_pass)
        self.assertFalse(score_response(json_case, '```json\n{"fastapi":"redirect test"}\n```').required_pass)

        path_case = dict(self.case, forbidden_adoption_terms=[], allowed_paths=["app/parser.py"])
        self.assertTrue(score_response(path_case, "Edit app/parser.py for fastapi redirect tests.").required_pass)
        self.assertFalse(
            score_response(path_case, "Edit app/parser.py and pyproject.toml for fastapi redirect tests.").required_pass
        )

    def test_case_set_has_required_coverage(self) -> None:
        cases = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 30)
        self.assertGreaterEqual(sum(case["language"] == "zh" for case in cases), 15)
        self.assertGreaterEqual(sum(case["language"] == "en" for case in cases), 15)
        self.assertTrue(any(case["soft_preference"] for case in cases))
        self.assertEqual(sum(case["category"] == "hard_constraint" for case in cases), 12)
        self.assertEqual(sum(case["category"] == "soft_preference" for case in cases), 6)
        self.assertEqual(sum(case["category"] == "safety_or_explicit_enforcement" for case in cases), 6)
        self.assertEqual(sum(case["category"] == "output_or_architecture_constraint" for case in cases), 6)
        self.assertTrue(any(case.get("required_enforcement_patterns") for case in cases))
        self.assertTrue(any(case.get("forbidden_adoption_terms") for case in cases))


if __name__ == "__main__":
    unittest.main()
