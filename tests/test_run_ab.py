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

    def test_signature_records_isolated_runner_protocol(self) -> None:
        case = {"prompt": "Design a service."}
        first = run_ab.evaluation_signature(case, "baseline", "model", None, "medium")
        second = run_ab.evaluation_signature(case, "baseline", "model", None, "medium")
        self.assertEqual(first, second)
        self.assertIn("isolated-codex-home", run_ab.RUNNER_PROTOCOL)


if __name__ == "__main__":
    unittest.main()
