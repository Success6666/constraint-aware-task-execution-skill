from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import run_ab  # noqa: E402


class RunAbTests(unittest.TestCase):
    def test_codex_home_contains_only_copied_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            source_home = temp_path / "source"
            source_home.mkdir()
            (source_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            (source_home / "AGENTS.md").write_text("must not be copied", encoding="utf-8")
            isolated_root = temp_path / "isolated"

            with (
                patch.dict(os.environ, {"CODEX_HOME": str(source_home)}),
                patch.object(run_ab, "CODEX_HOMES_PATH", isolated_root),
            ):
                codex_home = run_ab.prepare_codex_home("case", "baseline")

            self.assertEqual((codex_home / "auth.json").read_text(encoding="utf-8"), '{"token":"test"}')
            self.assertFalse((codex_home / "AGENTS.md").exists())
            self.assertEqual([path.name for path in codex_home.iterdir()], ["auth.json"])

    def test_prompt_requires_answer_only_behavior(self) -> None:
        prompt = run_ab.build_prompt({"prompt": "Design a service."}, "baseline")
        self.assertIn("answer-only design task", prompt)
        self.assertIn("do not inspect, create, edit, or patch files", prompt.casefold())

    def test_skill_prompt_keeps_user_request_unwrapped(self) -> None:
        prompt = run_ab.build_prompt({"prompt": "Design a service."}, "skill")
        self.assertTrue(prompt.startswith("Design a service."))
        self.assertNotIn("Complete every positive requirement explicitly", prompt)
        self.assertNotIn("$constraint-exec", prompt)
        self.assertEqual(prompt.count("Design a service."), 1)

    def test_signature_records_isolated_runner_protocol(self) -> None:
        case = {"prompt": "Design a service."}
        first = run_ab.evaluation_signature(
            case, "baseline", "model", None, "medium", "low", "responses"
        )
        second = run_ab.evaluation_signature(
            case, "baseline", "model", None, "medium", "low", "responses"
        )
        self.assertEqual(first, second)
        self.assertIn("isolated-codex-home", run_ab.RUNNER_PROTOCOL)

    def test_ablation_variants_have_distinct_prompt_contracts(self) -> None:
        case = {"prompt": "Design a service."}
        prompts = {
            variant: run_ab.build_prompt(case, variant)
            for variant in ("baseline", "skill", "structured-plan", "plan-validation", "full-v2")
        }
        self.assertEqual(prompts["baseline"], prompts["skill"])
        self.assertIn("JSON execution plan", prompts["structured-plan"])
        self.assertIn("deterministic validation", prompts["plan-validation"])
        self.assertIn("targeted repair levels", prompts["full-v2"])

    def test_trace_usage_is_normalized_with_strict_total(self) -> None:
        trace = "\n".join((
            '{"type":"turn.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,'
            '"output_tokens":25,"reasoning_output_tokens":5}}',
        ))
        self.assertEqual(run_ab.parse_trace_usage(trace), {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 25,
            "reasoning_output_tokens": 5,
            "total_tokens": 125,
        })

    def test_trace_usage_requires_completed_turn(self) -> None:
        self.assertIsNone(run_ab.parse_trace_usage('{"type":"turn.started"}'))

    def test_responses_payload_extracts_text_and_usage(self) -> None:
        response, usage = run_ab.parse_response_payload({
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "answer"}],
            }],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 25,
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        })
        self.assertEqual(response, "answer")
        self.assertEqual(usage["total_tokens"], 125)
        self.assertEqual(usage["cached_input_tokens"], 20)

    def test_output_root_can_isolate_experiments(self) -> None:
        with patch.object(sys, "argv", [
            "run_ab.py", "--output-root", "isolated-results", "--transport", "responses",
        ]):
            args = run_ab.parse_args()
        self.assertEqual(args.output_root, Path("isolated-results"))
        self.assertEqual(args.transport, "responses")

    def test_runner_protocol_disables_remote_plugins(self) -> None:
        self.assertIn("developer-instructions", run_ab.RUNNER_PROTOCOL)
        self.assertEqual(run_ab.PROVIDER_REQUEST_RETRIES, 0)
        self.assertEqual(run_ab.PROVIDER_STREAM_RETRIES, 0)


if __name__ == "__main__":
    unittest.main()
