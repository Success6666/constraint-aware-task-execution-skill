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
from run_runtime import apply_artifact_bundle, project_paths, runtime_prompt, validate_runtime  # noqa: E402


def plan(schema_version: str = "1.0") -> dict:
    return {
        "schema_version": schema_version,
        "objective": "Create config.json",
        "requirements": [{
            "id": "deliverable", "statement": "Create config.json",
            "acceptance_criteria": ["config.json contains the requested data"],
        }],
        "hard_constraints": [{
            "id": "path", "type": "hard", "statement": "Only config.json",
            "strategy": "Write config.json", "required_gate": False, "failure_action": "",
        }],
        "soft_preferences": [], "risk_points": [],
        "artifacts": [{"path": "config.json", "kind": "json"}],
        "validation_profile": {"validators": [{"type": "json_schema"}]},
    }


class RuntimeValidationTests(unittest.TestCase):
    def test_explicit_runtime_gate_is_allowed_by_plan_context(self) -> None:
        case = next(
            item
            for item in run_runtime.load_cases(None)
            if item["id"] == "upload-signature-guard"
        )
        self.assertTrue(case["required_gate"])

    def test_artifact_bundle_schema_is_strict(self) -> None:
        schema = json.loads(run_runtime.ARTIFACT_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))

        def assert_strict(value: object) -> None:
            if isinstance(value, dict):
                properties = value.get("properties")
                if isinstance(properties, dict):
                    self.assertEqual(set(value.get("required", [])), set(properties))
                    self.assertFalse(value.get("additionalProperties", True))
                for child in value.values():
                    assert_strict(child)
            elif isinstance(value, list):
                for child in value:
                    assert_strict(child)

        assert_strict(schema)

    def test_runtime_workspace_and_prompt_require_read_only_bundle(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "files_exist", "paths": ["config.json"]}],
        }

        with tempfile.TemporaryDirectory() as temp:
            workspace = run_runtime.prepare_workspace(
                Path(temp), "model", 1, run_runtime.VARIANTS["baseline"], "runtime-config",
                workspace_rules=run_runtime.RUNTIME_WORKSPACE_RULES,
            )
            rules = (workspace / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("inspect or modify the workspace", rules)
        self.assertIn("Do not call tools", rules)
        self.assertIn("current prompt and output schema", rules)
        self.assertIn("runner will validate and write the bundle", runtime_prompt(case, "direct"))
        self.assertIn("Do not call tools", runtime_prompt(case, "direct"))
        self.assertIn(
            "runner will validate and write the bundle",
            runtime_prompt(case, "full-v2", errors=["FILE_MISSING:config.json"]),
        )

    def test_full_v2_retries_plan_validation_before_execution(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "json_exact", "path": "config.json", "value": {"ok": True}}],
        }
        plan_calls = 0
        artifact_calls: list[tuple[Path | None, str]] = []

        def fake_run_codex(_prompt, _model, _effort, workspace, _temp, output_path, _trace, _timeout,
                           output_schema=None, sandbox="read-only"):
            nonlocal plan_calls
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_schema == run_runtime.OUTPUT_SCHEMA_PATH:
                plan_calls += 1
                output_path.write_text(json.dumps(plan("0" if plan_calls == 1 else "1.0")), encoding="utf-8")
            else:
                artifact_calls.append((output_schema, sandbox))
                output_path.write_text(
                    json.dumps({"files": [{"path": "config.json", "content": '{"ok": true}'}]}),
                    encoding="utf-8",
                )
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
        self.assertEqual(artifact_calls, [(run_runtime.ARTIFACT_BUNDLE_SCHEMA_PATH, "read-only")])

    def test_direct_execution_uses_read_only_artifact_bundle(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "json_exact", "path": "config.json", "value": {"ok": True}}],
        }
        calls: list[tuple[Path | None, str]] = []

        def fake_run_codex(_prompt, _model, _effort, _workspace, _temp, output_path, _trace,
                           _timeout, output_schema=None, sandbox="read-only"):
            calls.append((output_schema, sandbox))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps({
                "files": [{"path": "config.json", "content": '{"ok": true}'}],
            }), encoding="utf-8")
            return {
                "success": True, "returncode": 0, "error": "", "elapsed_seconds": 0.1,
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                          "reasoning_output_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(
            run_runtime, "run_codex", side_effect=fake_run_codex
        ):
            result = run_runtime.run_runtime_job(
                case, "direct", "model", 1, Path(temp), "medium", 30, 0,
            )

        self.assertTrue(result["contract_pass"])
        self.assertEqual(calls, [(run_runtime.ARTIFACT_BUNDLE_SCHEMA_PATH, "read-only")])

    def test_full_v2_repairs_with_another_read_only_bundle(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "json_exact", "path": "config.json", "value": {"ok": True}}],
        }
        artifact_calls = 0

        def fake_run_codex(prompt_text, _model, _effort, _workspace, _temp, output_path, _trace,
                           _timeout, output_schema=None, sandbox="read-only"):
            nonlocal artifact_calls
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_schema == run_runtime.OUTPUT_SCHEMA_PATH:
                output_path.write_text(json.dumps(plan()), encoding="utf-8")
            else:
                artifact_calls += 1
                value = True if "VALIDATION_ERRORS" in prompt_text else False
                output_path.write_text(json.dumps({
                    "files": [{"path": "config.json", "content": json.dumps({"ok": value})}],
                }), encoding="utf-8")
                self.assertEqual(output_schema, run_runtime.ARTIFACT_BUNDLE_SCHEMA_PATH)
                self.assertEqual(sandbox, "read-only")
            return {
                "success": True, "returncode": 0, "error": "", "elapsed_seconds": 0.1,
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
                          "reasoning_output_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as temp, patch.object(
            run_runtime, "run_codex", side_effect=fake_run_codex
        ):
            result = run_runtime.run_runtime_job(
                case, "full-v2", "model", 1, Path(temp), "medium", 30, 1, 1,
            )

        self.assertTrue(result["contract_pass"])
        self.assertTrue(result["repair_success"])
        self.assertEqual(artifact_calls, 2)

    def test_artifact_bundle_writes_allowed_files_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            bundle = root / "bundle.json"
            bundle.write_text(json.dumps({"files": [
                {"path": "app/main.py", "content": "print('ok')\n"},
                {"path": "README.md", "content": "# Ready\n"},
            ]}), encoding="utf-8")

            applied, errors = apply_artifact_bundle(
                bundle, workspace, ["app/main.py", "README.md"]
            )

            self.assertEqual(errors, [])
            self.assertEqual(applied, ["app/main.py", "README.md"])
            self.assertEqual((workspace / "app" / "main.py").read_text(encoding="utf-8"), "print('ok')\n")
            self.assertFalse(any(path.suffix == ".tmp" for path in workspace.rglob("*")))

    def test_artifact_bundle_rejects_unsafe_paths_without_partial_writes(self) -> None:
        invalid_paths = ["../escape.txt", "/absolute.txt", "C:/drive.txt", "nested\\file.txt"]
        for invalid in invalid_paths:
            with self.subTest(path=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                bundle = root / "bundle.json"
                bundle.write_text(json.dumps({"files": [
                    {"path": "safe.txt", "content": "safe"},
                    {"path": invalid, "content": "escape"},
                ]}), encoding="utf-8")

                applied, errors = apply_artifact_bundle(bundle, workspace, ["safe.txt"])

                self.assertEqual(applied, [])
                self.assertTrue(any(error.startswith("ARTIFACT_PATH_INVALID") for error in errors))
                self.assertFalse((workspace / "safe.txt").exists())

    def test_artifact_bundle_rejects_duplicate_unallowed_and_oversized_files(self) -> None:
        cases = [
            ([{"path": "file.txt", "content": "a"}, {"path": "FILE.txt", "content": "b"}],
             ["file.txt", "FILE.txt"], "ARTIFACT_PATH_DUPLICATE"),
            ([{"path": "other.txt", "content": "a"}], ["file.txt"], "ARTIFACT_PATH_NOT_ALLOWED"),
            ([{"path": "file.txt", "content": "12345"}], ["file.txt"], "ARTIFACT_FILE_TOO_LARGE"),
        ]
        for files, allowed, expected in cases:
            with self.subTest(error=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                bundle = root / "bundle.json"
                bundle.write_text(json.dumps({"files": files}), encoding="utf-8")
                limit = 4 if expected == "ARTIFACT_FILE_TOO_LARGE" else run_runtime.MAX_ARTIFACT_FILE_BYTES
                with patch.object(run_runtime, "MAX_ARTIFACT_FILE_BYTES", limit):
                    applied, errors = apply_artifact_bundle(bundle, workspace, allowed)
                self.assertEqual(applied, [])
                self.assertTrue(any(error.startswith(expected) for error in errors))

    def test_artifact_bundle_rejects_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            bundle = root / "bundle.json"
            bundle.write_text(json.dumps({
                "files": [{"path": "linked/file.txt", "content": "blocked"}],
            }), encoding="utf-8")
            path_type = type(workspace)
            original = path_type.is_symlink

            def fake_is_symlink(path: Path) -> bool:
                return path == workspace / "linked" or original(path)

            with patch.object(path_type, "is_symlink", autospec=True, side_effect=fake_is_symlink):
                applied, errors = apply_artifact_bundle(
                    bundle, workspace, ["linked/file.txt"]
                )

            self.assertEqual(applied, [])
            self.assertEqual(errors, ["ARTIFACT_SYMLINK:linked/file.txt"])

    def test_artifact_bundle_rejects_total_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            bundle = root / "bundle.json"
            bundle.write_text(json.dumps({"files": [
                {"path": "one.txt", "content": "123"},
                {"path": "two.txt", "content": "456"},
            ]}), encoding="utf-8")
            with patch.object(run_runtime, "MAX_ARTIFACT_BUNDLE_BYTES", 5):
                applied, errors = apply_artifact_bundle(
                    bundle, workspace, ["one.txt", "two.txt"]
                )

            self.assertEqual(applied, [])
            self.assertEqual(errors, ["ARTIFACT_BUNDLE_TOO_LARGE"])

    def test_runtime_signature_tracks_bundle_schema(self) -> None:
        case = {
            "id": "config", "prompt": "Create config.json", "allowed_paths": ["config.json"],
            "validators": [{"type": "files_exist", "paths": ["config.json"]}],
        }
        before = run_runtime.runtime_signature(case, "direct", "model", 1, "medium")
        with tempfile.TemporaryDirectory() as temp:
            alternate = Path(temp) / "schema.json"
            alternate.write_text('{"type":"object"}', encoding="utf-8")
            with patch.object(run_runtime, "ARTIFACT_BUNDLE_SCHEMA_PATH", alternate):
                after = run_runtime.runtime_signature(case, "direct", "model", 1, "medium")

        self.assertNotEqual(before, after)

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

    def test_command_validator_requires_observable_test_execution(self) -> None:
        case = {
            "allowed_paths": [],
            "validators": [{
                "type": "command",
                "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                "output_patterns": [r"Ran [1-9][0-9]* test"],
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "tests").mkdir()
            results = validate_runtime(case, workspace)
        self.assertEqual(results[1]["status"], "fail")
        self.assertTrue(any("COMMAND_OUTPUT_MISSING" in error for error in results[1]["errors"]))

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
