from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from protocol import (  # noqa: E402
    ExecutionProtocol,
    RetryTelemetry,
    choose_retry,
    detect_gate_relation,
    merge_plan_validations,
    normalize_plan_validation_issues,
    parse_plan,
    synthesize_minimal_plan,
    validate_artifact,
    validate_markdown_artifact,
    validate_path_scope,
    validate_plan,
    validate_plan_context,
    validate_python_artifact,
)
from ablation import summarize  # noqa: E402


def valid_plan() -> dict:
    return {
        "schema_version": "1.0",
        "objective": "Build a tested API",
        "hard_constraints": [{
            "constraint": "Do not use Redis",
            "implementation_strategy": "Use SQLite-backed persistence",
            "enforcement_required": False,
        }],
        "soft_preferences": [{"preference": "Prefer few dependencies", "tradeoff": "Use stdlib where practical"}],
        "risk_points": ["Concurrent writes"],
        "artifacts": [{"path": "app.py", "kind": "python"}],
        "validation_profile": {"validators": [{"type": "python"}]},
    }


class ProtocolTests(unittest.TestCase):
    def test_valid_plan_and_json_round_trip(self) -> None:
        plan, result = parse_plan(json.dumps(valid_plan()))
        self.assertIsNotNone(plan)
        self.assertTrue(result.valid)
        self.assertEqual(result.to_dict()["issues"], [])

    def test_canonical_constraint_aliases_are_supported(self) -> None:
        plan = valid_plan()
        plan["hard_constraints"] = [{
            "id": "no-redis",
            "type": "hard",
            "statement": "Do not use Redis",
            "strategy": "Use SQLite-backed persistence",
            "required_gate": False,
        }]
        self.assertTrue(validate_plan(plan).valid)

    def test_plan_rejects_empty_objective_and_unrequested_gate(self) -> None:
        plan = valid_plan()
        plan["objective"] = ""
        plan["hard_constraints"][0]["implementation_strategy"] = (
            "Create a Redis scanner and fail CI if Redis appears"
        )
        result = validate_plan(plan)
        codes = {issue.code for issue in result.issues}
        self.assertIn("OBJECTIVE_REQUIRED", codes)
        self.assertIn("UNREQUESTED_FAILURE_GATE", codes)

    def test_explicit_enforcement_requires_failure_action(self) -> None:
        plan = valid_plan()
        constraint = plan["hard_constraints"][0]
        constraint["enforcement_required"] = True
        result = validate_plan(plan)
        self.assertIn("FAILURE_ACTION_REQUIRED", {issue.code for issue in result.issues})
        constraint["failure_action"] = "Reject the build"
        self.assertTrue(validate_plan(plan).valid)

    def test_context_rejects_runner_constraints_and_unrequested_gates(self) -> None:
        plan = valid_plan()
        plan["hard_constraints"] = [
            {
                "id": "no-redis",
                "type": "hard",
                "statement": "Do not use Redis",
                "strategy": "Use PostgreSQL",
                "required_gate": True,
                "failure_action": "Reject Redis dependencies",
            },
            {
                "id": "schema-output",
                "type": "hard",
                "statement": "Return only the planning schema",
                "strategy": "Emit JSON",
                "required_gate": False,
                "failure_action": "",
            },
        ]
        result = validate_plan_context(
            plan,
            ["redis"],
            required_gates_allowed=False,
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn("UNREQUESTED_REQUIRED_GATE", codes)
        self.assertIn("CONSTRAINT_NOT_GROUNDED", codes)

    def test_context_allows_explicit_enforcement_gate(self) -> None:
        plan = valid_plan()
        plan["hard_constraints"] = [{
            "id": "executable-upload",
            "type": "enforcement",
            "statement": "Reject executable uploads",
            "strategy": "Inspect file signatures",
            "required_gate": True,
            "failure_action": "Reject the upload",
        }]
        result = validate_plan_context(
            plan,
            ["executable"],
            required_gates_allowed=True,
        )
        self.assertTrue(result.valid)

    def test_normalization_is_limited_to_reported_plan_paths(self) -> None:
        plan = valid_plan()
        plan["hard_constraints"] = [
            {
                "id": "redis",
                "type": "enforcement",
                "statement": "Do not use Redis",
                "strategy": "Use SQLite",
                "required_gate": False,
                "failure_action": "",
            },
            {
                "id": "invented",
                "type": "hard",
                "statement": "Require Alembic",
                "strategy": "Add migrations",
                "required_gate": False,
                "failure_action": "",
            },
        ]
        validation = merge_plan_validations(
            validate_plan(plan),
            validate_plan_context(
                plan,
                ["redis"],
                required_gates_allowed=False,
            ),
        )
        normalized, changes = normalize_plan_validation_issues(plan, validation)
        self.assertEqual(len(normalized["hard_constraints"]), 1)
        self.assertEqual(normalized["hard_constraints"][0]["type"], "hard")
        self.assertFalse(normalized["hard_constraints"][0]["required_gate"])
        self.assertEqual(
            set(changes),
            {"reclassified:hard_constraints[0]", "removed:hard_constraints[1]"},
        )
        self.assertEqual(plan["hard_constraints"][0]["type"], "enforcement")

    def test_normalization_removes_soft_preference_hardening(self) -> None:
        plan = valid_plan()
        validation = validate_plan_context(
            plan,
            ["dependencies"],
            required_gates_allowed=False,
            soft_preference_only=True,
        )
        normalized, changes = normalize_plan_validation_issues(plan, validation)
        self.assertEqual(normalized["hard_constraints"], [])
        self.assertIn("removed:hard_constraints[0]", changes)

    def test_context_requires_every_user_constraint_and_soft_preference(self) -> None:
        plan = valid_plan()
        plan["hard_constraints"] = []
        missing_constraint = validate_plan_context(
            plan,
            ["executable"],
            required_gates_allowed=True,
        )
        self.assertEqual(
            {issue.code for issue in missing_constraint.issues},
            {"USER_CONSTRAINT_MISSING", "REQUIRED_GATE_NOT_PLANNED"},
        )

        plan["soft_preferences"] = [{
            "type": "soft",
            "preference": "Prefer fewer dependencies",
            "tradeoff": "Use a dependency when it removes material risk",
        }]
        soft = validate_plan_context(
            plan,
            ["dependencies"],
            required_gates_allowed=False,
            soft_preference_only=True,
        )
        self.assertTrue(soft.valid)

    def test_artifact_validators_have_pass_fail_and_unsupported_states(self) -> None:
        self.assertEqual(validate_artifact("json", '{"ok": true}').status, "pass")
        self.assertEqual(validate_artifact("json", '{"ok":').status, "fail")
        self.assertEqual(validate_artifact("binary", b"x").status, "unsupported")
        self.assertEqual(validate_markdown_artifact("# Plan", ["Plan"]).status, "pass")
        self.assertEqual(validate_python_artifact("def broken(:").status, "fail")

    def test_path_scope_is_deterministic(self) -> None:
        self.assertEqual(validate_path_scope(["app.py"], ["app.py"]).status, "pass")
        self.assertEqual(validate_path_scope(["app.py", "secret.env"], ["app.py"]).status, "fail")

    def test_retry_escalates_from_local_to_plan_and_stops(self) -> None:
        self.assertEqual(choose_retry(["ARTIFACT_PATH_REQUIRED"], 0, 3).level, "level_1")
        self.assertEqual(choose_retry(["SCHEMA_VERSION"], 1, 3).level, "level_3")
        self.assertEqual(choose_retry(["OTHER"], 3, 3).level, "stop")

    def test_retry_telemetry_is_machine_readable(self) -> None:
        telemetry = RetryTelemetry()
        telemetry.record(choose_retry(["ARTIFACT_PATH_REQUIRED"], 0, 3), True)
        telemetry.record(choose_retry(["SCHEMA_VERSION"], 1, 3), False)
        metrics = telemetry.metrics()
        self.assertEqual(metrics["retry_rate"], 2)
        self.assertEqual(metrics["repair_success_rate"], 0.5)
        self.assertEqual(metrics["plan_retry_rate"], 1)
        self.assertEqual(metrics["artifact_retry_rate"], 1)

    def test_ablation_summary_keeps_missing_variants_explicit(self) -> None:
        rows = [{
            "variant": "baseline",
            "success": True,
            "score": {
                "evaluation_pass": True,
                "objective_coverage": 1.0,
                "constraint_adherence": 1.0,
                "overoptimization_score": 2.0,
                "under_enforcement_hits": 0,
            },
        }]
        summary = {item.variant: item for item in summarize(rows)}
        self.assertEqual(summary["baseline"].completed, 1)
        self.assertEqual(summary["full-v2"].completed, 0)

    def test_execution_protocol_facade_keeps_retry_state(self) -> None:
        runtime = ExecutionProtocol(max_attempts=2)
        self.assertTrue(runtime.validate_plan(valid_plan()).valid)
        self.assertEqual(runtime.validate_artifact("json", "{}").status, "pass")
        self.assertEqual(runtime.next_retry(["ARTIFACT_PATH_REQUIRED"]).level, "level_1")
        self.assertEqual(runtime.next_retry(["SCHEMA_VERSION"]).level, "level_3")
        self.assertEqual(runtime.next_retry(["OTHER"]).level, "stop")
        self.assertEqual(runtime.metrics()["retry_rate"], 2)

    def test_gate_relation_handles_negation_and_quotes(self) -> None:
        self.assertFalse(detect_gate_relation("Do not add a Redis scanner or reject the build."))
        self.assertFalse(detect_gate_relation("The README quotes 'RedisDetector would fail CI' as an anti-pattern."))
        self.assertFalse(detect_gate_relation("不要因为文档提到 Redis 就自动拒绝任务。"))
        self.assertTrue(detect_gate_relation("Add a Redis scanner that rejects the build when it appears."))

    def test_synthesize_minimal_plan_is_grounded_and_preserves_softness(self) -> None:
        hard_case = {
            "prompt": "Build a service without Redis.",
            "constraint_terms": ["Redis"],
            "soft_preference": False,
        }
        synthesized, changes = synthesize_minimal_plan(hard_case, None)
        self.assertTrue(changes)
        self.assertEqual(synthesized["hard_constraints"][0]["type"], "hard")
        self.assertFalse(synthesized["hard_constraints"][0]["required_gate"])
        validation = merge_plan_validations(
            validate_plan(synthesized),
            validate_plan_context(synthesized, ["Redis"], required_gates_allowed=False),
        )
        self.assertTrue(validation.valid)

        soft_case = {
            "prompt": "Plan a service. Prefer fewer dependencies.",
            "constraint_terms": ["dependencies"],
            "soft_preference": True,
        }
        soft_plan, soft_changes = synthesize_minimal_plan(soft_case, None)
        self.assertTrue(soft_changes)
        self.assertEqual(soft_plan["soft_preferences"][0]["type"], "soft")
        self.assertEqual(soft_plan["hard_constraints"], [])

    def test_synthesize_minimal_plan_requires_frozen_case_terms(self) -> None:
        plan, changes = synthesize_minimal_plan({"prompt": "Do work."}, None)
        self.assertEqual(plan, {})
        self.assertEqual(changes, ())


if __name__ == "__main__":
    unittest.main()
