from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import run_matrix  # noqa: E402
from experiment_variants import VARIANTS, select_variants  # noqa: E402


def plan(required_gate: bool = False, strategy: str = "Use SQLite", failure_action: str = "") -> dict:
    return {
        "schema_version": "1.0",
        "objective": "Return a JSON implementation plan",
        "hard_constraints": [{
            "id": "constraint-1",
            "type": "enforcement" if required_gate else "hard",
            "statement": "Return JSON only",
            "strategy": strategy,
            "required_gate": required_gate,
            "failure_action": failure_action,
        }],
        "soft_preferences": [],
        "risk_points": [],
        "artifacts": [{"path": "response.json", "kind": "json"}],
        "validation_profile": {"validators": [{"type": "json"}]},
    }


class RunMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = {
            "id": "json-case",
            "prompt": "Return one JSON object with components and tests.",
            "constraint_terms": ["json"],
            "objective_markers": ["components", "tests"],
            "required_response_format": "json_object",
            "soft_preference": False,
        }

    def test_variants_are_orthogonal_and_selection_is_stable(self) -> None:
        self.assertEqual(len(VARIANTS), 8)
        self.assertFalse(VARIANTS["positive-framing-only"].use_skill)
        self.assertTrue(VARIANTS["v1-full"].use_skill)
        self.assertFalse(VARIANTS["structured-plan-only"].validate_plan)
        self.assertTrue(VARIANTS["plan-validation"].validate_plan)
        self.assertTrue(VARIANTS["full-v2"].repair_artifact)
        selected = select_variants(["full-v2", "baseline", "full-v2"])
        self.assertEqual([variant.name for variant in selected], ["full-v2", "baseline"])

    def test_workspace_installs_only_variant_specific_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = run_matrix.prepare_workspace(root, "model", 1, VARIANTS["baseline"], "case")
            skilled = run_matrix.prepare_workspace(root, "model", 1, VARIANTS["v1-full"], "case")
            self.assertFalse((baseline / ".codex" / "skills").exists())
            skill_path = skilled / ".codex" / "skills" / "constraint-aware-task-execution" / "SKILL.md"
            self.assertTrue(skill_path.is_file())
            self.assertIn("primary objective", skill_path.read_text(encoding="utf-8"))

    def test_resume_requires_evidence_and_skill_digest_changes_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            answer = root / "raw" / "answers" / "answer.md"
            answer.parent.mkdir(parents=True)
            answer.write_text("answer", encoding="utf-8")
            row = {"evidence": {"answer": "raw/answers/answer.md"}}
            self.assertTrue(run_matrix.evidence_exists(root, row, VARIANTS["baseline"]))
            self.assertFalse(run_matrix.evidence_exists(root, row, VARIANTS["full-v2"]))
            original = run_matrix.SKILL_PATH
            skill = root / "SKILL.md"
            skill.write_text("one", encoding="utf-8")
            try:
                run_matrix.SKILL_PATH = skill
                before = run_matrix.signature(self.case, VARIANTS["v1-full"], "model", 1, "medium")
                baseline_before = run_matrix.signature(self.case, VARIANTS["baseline"], "model", 1, "medium")
                skill.write_text("two", encoding="utf-8")
                after = run_matrix.signature(self.case, VARIANTS["v1-full"], "model", 1, "medium")
                baseline_after = run_matrix.signature(self.case, VARIANTS["baseline"], "model", 1, "medium")
            finally:
                run_matrix.SKILL_PATH = original
            self.assertNotEqual(before, after)
            self.assertEqual(baseline_before, baseline_after)

    def test_repair_prompt_contains_machine_errors_but_no_scores(self) -> None:
        prompt_text = run_matrix.repair_prompt(
            self.case, VARIANTS["full-v2"], json.dumps(plan()), "bad answer",
            ["ARTIFACT_RESPONSE_FORMAT"], {"next_action": "repair_section"},
        )
        self.assertIn("ARTIFACT_RESPONSE_FORMAT", prompt_text)
        self.assertNotIn("overoptimization_score", prompt_text)
        self.assertNotIn("objective_coverage", prompt_text)

    def test_full_v2_runs_plan_execution_and_targeted_repair(self) -> None:
        calls: list[str] = []

        def fake_run_codex(prompt_text: str, _model: str, _effort: str, _workspace: Path,
                           _temp_root: Path, output_path: Path, _trace_path: Path,
                           _timeout: int, output_schema: Path | None = None) -> dict:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_schema:
                output_path.write_text(json.dumps(plan()), encoding="utf-8")
                calls.append("plan")
            elif "PREVIOUS_ANSWER" in prompt_text:
                output_path.write_text('{"components": [], "tests": []}', encoding="utf-8")
                calls.append("repair")
            else:
                output_path.write_text('```json\n{"components": [], "tests": []}\n```', encoding="utf-8")
                calls.append("execute")
            return {
                "success": True, "returncode": 0, "error": "", "elapsed_seconds": 0.1,
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                          "reasoning_output_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(run_matrix, "run_codex", side_effect=fake_run_codex):
            result = run_matrix.run_job(
                self.case, VARIANTS["full-v2"], "model", 1, Path(temp), "medium", 30, 2, 2,
            )
        self.assertEqual(calls, ["plan", "execute", "repair"])
        self.assertTrue(result["success"])
        self.assertTrue(result["artifact_contract_pass"])
        self.assertEqual(result["artifact_retry_count"], 1)
        self.assertTrue(result["repair_success"])

    def test_plan_validation_retries_invalid_gate_plan(self) -> None:
        plan_calls = 0

        def fake_run_codex(prompt_text: str, _model: str, _effort: str, _workspace: Path,
                           _temp_root: Path, output_path: Path, _trace_path: Path,
                           _timeout: int, output_schema: Path | None = None) -> dict:
            nonlocal plan_calls
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_schema:
                plan_calls += 1
                payload = plan(strategy="Create a Redis scanner and fail CI") if plan_calls == 1 else plan()
                output_path.write_text(json.dumps(payload), encoding="utf-8")
            else:
                output_path.write_text('{"components": [], "tests": []}', encoding="utf-8")
            return {
                "success": True, "returncode": 0, "error": "", "elapsed_seconds": 0.1,
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                          "reasoning_output_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(run_matrix, "run_codex", side_effect=fake_run_codex):
            result = run_matrix.run_job(
                self.case, VARIANTS["plan-validation"], "model", 1, Path(temp), "medium", 30, 2, 0,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["plan_retry_count"], 1)

    def test_unverifiable_artifact_is_explicitly_unsupported(self) -> None:
        case = {
            "id": "soft",
            "prompt": "Plan a service and prefer fewer dependencies.",
            "constraint_terms": ["dependencies"],
            "objective_markers": ["service"],
            "soft_preference": True,
        }
        status, errors, _score = run_matrix.artifact_errors(case, "Service plan")
        self.assertEqual(status, "unsupported")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
