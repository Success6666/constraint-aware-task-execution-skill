from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import run_runtime  # noqa: E402
from run_runtime import project_paths, validate_runtime  # noqa: E402


def plan(schema_version: str = "1.0") -> dict:
    return {
        "schema_version": schema_version,
        "objective": "Create config.json",
        "hard_constraints": [{
            "id": "path", "type": "hard", "statement": "Only config.json",
            "strategy": "Write config.json", "required_gate": False, "failure_action": "",
        }],
        "soft_preferences": [], "risk_points": [],
        "artifacts": [{"path": "config.json", "kind": "json"}],
        "validation_profile": {"validators": [{"type": "json"}]},
    }


class RuntimeValidationTests(unittest.TestCase):
    def test_full_v2_retries_plan_validation_before_execution(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "json_exact", "path": "config.json", "value": {"ok": True}}],
        }
        plan_calls = 0

        def fake_run_codex(_prompt, _model, _effort, workspace, _temp, output_path, _trace, _timeout,
                           output_schema=None, sandbox="read-only"):
            nonlocal plan_calls
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_schema:
                plan_calls += 1
                output_path.write_text(json.dumps(plan("0" if plan_calls == 1 else "1.0")), encoding="utf-8")
            else:
                (workspace / "config.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
                output_path.write_text("done", encoding="utf-8")
            return {
                "success": True, "returncode": 0, "error": "", "elapsed_seconds": 0.1,
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                          "reasoning_output_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(run_runtime, "run_codex", side_effect=fake_run_codex):
            result = run_runtime.run_runtime_job(
                case, "full-v2", "model", 1, Path(temp), "medium", 30, 1, 2,
            )
        self.assertTrue(result["contract_pass"])
        self.assertEqual(result["plan_retry_count"], 1)
        self.assertEqual(plan_calls, 2)

    def test_json_and_path_contract(self) -> None:
        case = {
            "allowed_paths": ["config.json"],
            "validators": [
                {"type": "files_exist", "paths": ["config.json"]},
                {"type": "json_exact", "path": "config.json", "value": {"ok": True}},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "config.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
            self.assertTrue(all(result["status"] == "pass" for result in validate_runtime(case, workspace)))
            (workspace / "extra.txt").write_text("x", encoding="utf-8")
            self.assertEqual(validate_runtime(case, workspace)[0]["status"], "fail")

    def test_python_compile_forbidden_import_and_command(self) -> None:
        case = {
            "allowed_paths": ["report.py", "tests/test_report.py"],
            "validators": [
                {"type": "python_compile", "paths": ["report.py", "tests/test_report.py"]},
                {"type": "forbidden_imports", "paths": ["report.py"], "imports": ["pandas"]},
                {"type": "command", "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "tests").mkdir()
            (workspace / "report.py").write_text("def total():\n    return 1\n", encoding="utf-8")
            (workspace / "tests" / "test_report.py").write_text(
                "import unittest\nfrom report import total\n"
                "class T(unittest.TestCase):\n    def test_total(self): self.assertEqual(total(), 1)\n",
                encoding="utf-8",
            )
            results = validate_runtime(case, workspace)
            self.assertTrue(all(result["status"] == "pass" for result in results))
            (workspace / "report.py").write_text("import pandas\n", encoding="utf-8")
            self.assertTrue(any("FORBIDDEN_IMPORT" in error for result in validate_runtime(case, workspace) for error in result["errors"]))

    def test_project_paths_excludes_control_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "AGENTS.md").write_text("rules", encoding="utf-8")
            (workspace / ".codex" / "skills").mkdir(parents=True)
            (workspace / ".codex" / "skills" / "SKILL.md").write_text("control", encoding="utf-8")
            (workspace / "app.py").write_text("pass", encoding="utf-8")
            self.assertEqual(project_paths(workspace), {"app.py"})


if __name__ == "__main__":
    unittest.main()
